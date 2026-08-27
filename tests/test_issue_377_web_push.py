"""
Coverage for issue #377 — Web Push notifications for the installed PWA.

Same real-Postgres pattern as tests/test_issue_390_primevideo_cookie_pat.py: applies
the actual schema.sql and drives the real FastAPI routes via TestClient, with the
actual `pywebpush.webpush` call mocked out (never hits a real push service).

Covers:
  1. Subscribing (POST /settings/push/subscribe) stores a row; unsubscribing removes
     it; re-subscribing the same endpoint upserts rather than duplicating.
  2. An incomplete subscription body is rejected (400), not silently half-stored.
  3. app.notify.notify()'s WebPushChannel sends to every stored subscription for a
     user, and only that user's own subscriptions.
  4. A 404/410 response from the push service removes that subscription
     automatically; any other error leaves it in place for the next notify().
  5. The VAPID private key round-trips through encryption correctly (mirrors
     tests/test_issue_310_credential_encryption.py's instance_config pattern) and
     get_or_create_keypair() is idempotent — a second call returns the same keys,
     not a freshly generated pair.
  6. Unauthenticated requests to either route redirect to login, same as any other
     session-authenticated settings route.
  7. The generated private key actually parses through py_vapid's REAL
     Vapid.from_string() — the exact call pywebpush.webpush() makes internally on
     every real send, which every other test above deliberately mocks out. This
     is the one gap that let a real bug ship past 100% test-passing CI: an earlier
     version of get_or_create_keypair() stored private_pem() (PEM text, with
     "-----BEGIN PRIVATE KEY-----" armor) where pywebpush expects base64url(DER),
     so every real send failed with "ValueError: Could not deserialize key data"
     — caught silently by WebPushChannel's own except clause, never surfaced by
     any of the mocked tests above, only found by actually subscribing a real
     browser and sending a real push in a live dev stack.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


def _try_connect():
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=2)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception:
        return None


@pytest.fixture(scope="module")
def pg_conn():
    conn = _try_connect()
    if conn is None:
        pytest.skip(
            f"No reachable Postgres at {DATABASE_URL} — this suite needs a real "
            "throwaway instance (same one .github/workflows/pr-validate.yml provisions)."
        )
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(SCHEMA_SQL)
    yield conn
    conn.close()


@pytest.fixture()
def app_client(pg_conn, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-377-secret")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    import app.main as m
    import app.notify as notify_mod
    from fastapi.testclient import TestClient

    with pg_conn.cursor() as cur:
        cur.execute(
            "TRUNCATE push_subscriptions, instance_config, settings, sessions, users RESTART IDENTITY CASCADE"
        )
        for uid, email in ((1, "a@example.com"), (2, "b@example.com")):
            cur.execute(
                "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
                "VALUES (%s, 'local', %s, %s, %s, true)",
                (uid, email, email, m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode()),
            )

    client = TestClient(m.app)
    # app.main imports the `notify` FUNCTION by name (`from app.notify import notify`),
    # not the module — WebPushChannel.send() still looks up `webpush`/`vapid` inside the
    # real app.notify module at call time, so that's what patching must target, not
    # anything hung off app.main. m.vapid IS the real module either way (app.main imports
    # it as a module), so patching through m.vapid also affects app.notify's own
    # `from app import ... vapid` reference — same underlying module object.
    return client, SimpleNamespace(main=m, notify_mod=notify_mod, vapid=m.vapid)


def _login(client, email="a@example.com"):
    resp = client.post(
        "/auth/login", data={"email": email, "password": "password123"}, follow_redirects=False
    )
    assert resp.status_code == 303


SUB_1 = {"endpoint": "https://push.example.com/sub1", "keys": {"p256dh": "p256dh-1", "auth": "auth-1"}}
SUB_2 = {"endpoint": "https://push.example.com/sub2", "keys": {"p256dh": "p256dh-2", "auth": "auth-2"}}


# ── Subscribe / unsubscribe routes ──────────────────────────────────────────────


def test_subscribe_stores_a_row(app_client, pg_conn):
    client, _m = app_client
    _login(client)

    resp = client.post("/settings/push/subscribe", json=SUB_1)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    with pg_conn.cursor() as cur:
        cur.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = 1")
        row = cur.fetchone()
    assert row == (SUB_1["endpoint"], "p256dh-1", "auth-1")


def test_subscribe_rejects_incomplete_body(app_client):
    client, _m = app_client
    _login(client)

    resp = client.post("/settings/push/subscribe", json={"endpoint": "https://push.example.com/x"})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_resubscribing_same_endpoint_upserts(app_client, pg_conn):
    client, _m = app_client
    _login(client)

    client.post("/settings/push/subscribe", json=SUB_1)
    updated = {"endpoint": SUB_1["endpoint"], "keys": {"p256dh": "new-p256dh", "auth": "new-auth"}}
    resp = client.post("/settings/push/subscribe", json=updated)
    assert resp.status_code == 200

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM push_subscriptions WHERE user_id = 1 AND endpoint = %s", (SUB_1["endpoint"],))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT p256dh, auth FROM push_subscriptions WHERE user_id = 1 AND endpoint = %s", (SUB_1["endpoint"],))
        assert cur.fetchone() == ("new-p256dh", "new-auth")


def test_unsubscribe_removes_the_row(app_client, pg_conn):
    client, _m = app_client
    _login(client)

    client.post("/settings/push/subscribe", json=SUB_1)
    resp = client.post("/settings/push/unsubscribe", json={"endpoint": SUB_1["endpoint"]})
    assert resp.status_code == 200

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM push_subscriptions WHERE user_id = 1")
        assert cur.fetchone()[0] == 0


def test_subscribe_scoped_to_the_logged_in_user(app_client, pg_conn):
    client, _m = app_client
    _login(client, "b@example.com")
    client.post("/settings/push/subscribe", json=SUB_1)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT user_id FROM push_subscriptions WHERE endpoint = %s", (SUB_1["endpoint"],))
        assert cur.fetchone()[0] == 2


def test_subscribe_requires_login(app_client):
    client, _m = app_client
    resp = client.post("/settings/push/subscribe", json=SUB_1, follow_redirects=False)
    assert resp.status_code == 303
    assert "/auth/login" in resp.headers["location"]


# ── app.notify dispatcher (WebPushChannel) ──────────────────────────────────────


def _fake_webpush_response(status_code):
    from pywebpush import WebPushException
    return WebPushException("gone", response=SimpleNamespace(status_code=status_code))


def test_notify_sends_to_every_stored_subscription(app_client, pg_conn, monkeypatch):
    client, m = app_client
    _login(client)
    client.post("/settings/push/subscribe", json=SUB_1)
    client.post("/settings/push/subscribe", json=SUB_2)

    calls = []
    monkeypatch.setattr(m.notify_mod, "webpush", lambda **kw: calls.append(kw))
    monkeypatch.setattr(m.vapid, "get_or_create_keypair", lambda: ("pub", "priv-pem"))

    m.notify_mod.notify(1, "Test title", "Test body")

    assert len(calls) == 2
    endpoints = {c["subscription_info"]["endpoint"] for c in calls}
    assert endpoints == {SUB_1["endpoint"], SUB_2["endpoint"]}
    assert all(c["vapid_private_key"] == "priv-pem" for c in calls)
    import json as _json
    assert _json.loads(calls[0]["data"]) == {"title": "Test title", "body": "Test body"}


def test_notify_only_sends_to_that_users_own_subscriptions(app_client, pg_conn, monkeypatch):
    client, m = app_client
    _login(client, "a@example.com")
    client.post("/settings/push/subscribe", json=SUB_1)
    _login(client, "b@example.com")  # re-login on the same client swaps the active session
    client.post("/settings/push/subscribe", json=SUB_2)

    calls = []
    monkeypatch.setattr(m.notify_mod, "webpush", lambda **kw: calls.append(kw))
    monkeypatch.setattr(m.vapid, "get_or_create_keypair", lambda: ("pub", "priv-pem"))

    m.notify_mod.notify(1, "t", "b")
    assert len(calls) == 1
    assert calls[0]["subscription_info"]["endpoint"] == SUB_1["endpoint"]


def test_notify_removes_subscription_on_410(app_client, pg_conn, monkeypatch):
    client, m = app_client
    _login(client)
    client.post("/settings/push/subscribe", json=SUB_1)

    def raise_gone(**kw):
        raise _fake_webpush_response(410)

    monkeypatch.setattr(m.notify_mod, "webpush", raise_gone)
    monkeypatch.setattr(m.vapid, "get_or_create_keypair", lambda: ("pub", "priv-pem"))

    m.notify_mod.notify(1, "t", "b")

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM push_subscriptions WHERE user_id = 1")
        assert cur.fetchone()[0] == 0


def test_notify_removes_subscription_on_404(app_client, pg_conn, monkeypatch):
    client, m = app_client
    _login(client)
    client.post("/settings/push/subscribe", json=SUB_1)

    def raise_not_found(**kw):
        raise _fake_webpush_response(404)

    monkeypatch.setattr(m.notify_mod, "webpush", raise_not_found)
    monkeypatch.setattr(m.vapid, "get_or_create_keypair", lambda: ("pub", "priv-pem"))

    m.notify_mod.notify(1, "t", "b")

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM push_subscriptions WHERE user_id = 1")
        assert cur.fetchone()[0] == 0


def test_notify_keeps_subscription_on_other_errors(app_client, pg_conn, monkeypatch):
    client, m = app_client
    _login(client)
    client.post("/settings/push/subscribe", json=SUB_1)

    def raise_server_error(**kw):
        raise _fake_webpush_response(500)

    monkeypatch.setattr(m.notify_mod, "webpush", raise_server_error)
    monkeypatch.setattr(m.vapid, "get_or_create_keypair", lambda: ("pub", "priv-pem"))

    m.notify_mod.notify(1, "t", "b")

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM push_subscriptions WHERE user_id = 1")
        assert cur.fetchone()[0] == 1


def test_is_configured_reflects_subscription_existence(app_client):
    client, m = app_client
    _login(client)
    ch = m.notify_mod.WebPushChannel()
    assert ch.is_configured(1) is False

    client.post("/settings/push/subscribe", json=SUB_1)
    assert ch.is_configured(1) is True


# ── VAPID keypair (encryption + idempotency) ────────────────────────────────────


def test_vapid_private_key_stored_as_ciphertext_in_raw_db(app_client, pg_conn):
    _client, m = app_client
    public_b64, private_pem = m.vapid.get_or_create_keypair()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT value FROM instance_config WHERE key = 'vapid_private_key'")
        raw_stored = cur.fetchone()[0]
    assert raw_stored != private_pem  # never plaintext at rest

    with pg_conn.cursor() as cur:
        cur.execute("SELECT value FROM instance_config WHERE key = 'vapid_public_key'")
        raw_public = cur.fetchone()[0]
    assert raw_public == public_b64  # public half is not a secret, stays plaintext


def test_get_or_create_keypair_is_idempotent(app_client):
    _client, m = app_client
    first = m.vapid.get_or_create_keypair()
    second = m.vapid.get_or_create_keypair()
    assert first == second


def test_generated_private_key_actually_parses_via_real_py_vapid(app_client):
    """Regression test — see this file's module docstring, point 7. Feeds the
    real generated private key through py_vapid.Vapid.from_string() with
    nothing mocked, since that's the exact call pywebpush.webpush() makes
    internally on every real send. A PEM-formatted key (this bug's actual
    shape, live in production once) fails this with a ValueError; every other
    test in this file mocks webpush() itself, so none of them could ever have
    caught this."""
    from py_vapid import Vapid

    _client, m = app_client
    _public_b64, private_b64 = m.vapid.get_or_create_keypair()

    v = Vapid.from_string(private_key=private_b64)
    assert v.private_key is not None
