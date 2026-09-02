"""
Coverage for issue #390 — a PAT-authenticated write endpoint
(POST /api/pat/primevideo-cookie) letting a companion browser extension keep
Prime Video's stored cookie fresh without a manual copy-paste into Settings.

Same real-Postgres pattern as tests/test_issue_336_ha_status.py: applies the
actual schema.sql and drives the real FastAPI route via TestClient. Needs a
reachable Postgres via DATABASE_URL — skipped entirely if one isn't available.

Covers:
  1. Auth — missing/malformed/revoked bearer token all rejected with 401; a
     logged-in session with no bearer token is rejected too (PAT-only, no
     session fallback, same shape as _require_pat_user's HA route).
  2. Scope — a read-only PAT is rejected with 403; only read_write works.
  3. The write itself lands through config.py's encrypted-storage path (same
     one the session-based Settings route uses) and is scoped to the token's
     own user — one user's token can never write another user's cookie.
  4. Body validation — missing/blank cookie_header is a 400, not a silent no-op.
  5. The CSRF exemption issue #390 added to _csrf_protect() only fires for an
     actually-valid PAT — a garbage/unresolvable Authorization header must
     still hit the normal CSRF check, not bypass it.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

from app import config, pat  # noqa: E402  (needs sys.path insert above)


@pytest.fixture()
def app_client(pg_conn, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-390-secret")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    import app.main as m
    from fastapi.testclient import TestClient

    with pg_conn.cursor() as cur:
        cur.execute(
            "TRUNCATE personal_access_tokens, settings, sessions, users RESTART IDENTITY CASCADE"
        )
        for uid, email in ((1, "a@example.com"), (2, "b@example.com")):
            cur.execute(
                "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
                "VALUES (%s, 'local', %s, %s, %s, true)",
                (uid, email, email, m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode()),
            )

    _, rw_token_1 = pat.create_token(1, "extension", scope=pat.SCOPE_READ_WRITE)
    _, rw_token_2 = pat.create_token(2, "extension", scope=pat.SCOPE_READ_WRITE)
    _, ro_token_1 = pat.create_token(1, "read only", scope=pat.SCOPE_READ)
    revoked_id, revoked_token = pat.create_token(1, "revoked", scope=pat.SCOPE_READ_WRITE)
    pat.revoke_token(1, revoked_id)

    client = TestClient(m.app)
    return client, {
        "rw1": rw_token_1, "rw2": rw_token_2, "ro1": ro_token_1, "revoked": revoked_token,
    }


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ── Auth ─────────────────────────────────────────────────────────────────────


def test_missing_bearer_token_is_401(app_client):
    client, _ = app_client
    resp = client.post("/api/pat/primevideo-cookie", json={"cookie_header": "a=b"})
    assert resp.status_code == 401


def test_malformed_bearer_token_is_401(app_client):
    client, _ = app_client
    resp = client.post(
        "/api/pat/primevideo-cookie", json={"cookie_header": "a=b"},
        headers=_auth("not-a-real-token"),
    )
    assert resp.status_code == 401


def test_revoked_token_is_401(app_client):
    client, tokens = app_client
    resp = client.post(
        "/api/pat/primevideo-cookie", json={"cookie_header": "a=b"},
        headers=_auth(tokens["revoked"]),
    )
    assert resp.status_code == 401


def test_session_cookie_alone_is_not_accepted(app_client):
    """PAT-only by design — a logged-in browser session with no bearer token
    (and, since this is a POST, no CSRF token either) must still be rejected,
    not silently accepted because a session happens to exist."""
    client, _ = app_client
    login_resp = client.post(
        "/auth/login", data={"email": "a@example.com", "password": "password123"},
        follow_redirects=False,
    )
    assert login_resp.status_code == 303
    resp = client.post("/api/pat/primevideo-cookie", json={"cookie_header": "a=b"})
    assert resp.status_code in (401, 403)


# ── Scope ────────────────────────────────────────────────────────────────────


def test_read_only_token_is_rejected(app_client):
    client, tokens = app_client
    resp = client.post(
        "/api/pat/primevideo-cookie", json={"cookie_header": "a=b"},
        headers=_auth(tokens["ro1"]),
    )
    assert resp.status_code == 403


# ── The write itself ─────────────────────────────────────────────────────────


def test_valid_read_write_token_stores_the_cookie_encrypted(app_client, pg_conn):
    client, tokens = app_client
    resp = client.post(
        "/api/pat/primevideo-cookie",
        json={"cookie_header": "session-id=abc123; ubid-main=xyz789"},
        headers=_auth(tokens["rw1"]),
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # Stored via the same encrypted path the session-based Settings route uses —
    # the raw cookie string must never be sitting in plaintext in the DB.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM settings WHERE user_id = 1 AND key = 'primevideo_cookie_header'"
        )
        stored = cur.fetchone()[0]
    assert stored != "session-id=abc123; ubid-main=xyz789"
    assert config.get(1, "primevideo_cookie_header") == "session-id=abc123; ubid-main=xyz789"


def test_write_is_scoped_to_the_tokens_own_user(app_client):
    """user 2's token must only ever be able to write user 2's own cookie,
    never user 1's — there's no user_id in the request body, so this is really
    just confirming config.set_value() is called with the *resolved* user's id,
    not something a caller could spoof."""
    client, tokens = app_client
    resp = client.post(
        "/api/pat/primevideo-cookie", json={"cookie_header": "user2-cookie=1"},
        headers=_auth(tokens["rw2"]),
    )
    assert resp.status_code == 200
    assert config.get(2, "primevideo_cookie_header") == "user2-cookie=1"
    assert config.get(1, "primevideo_cookie_header") != "user2-cookie=1"


def test_overwrites_a_previously_stored_cookie(app_client):
    client, tokens = app_client
    client.post(
        "/api/pat/primevideo-cookie", json={"cookie_header": "old-cookie=1"},
        headers=_auth(tokens["rw1"]),
    )
    resp = client.post(
        "/api/pat/primevideo-cookie", json={"cookie_header": "new-cookie=2"},
        headers=_auth(tokens["rw1"]),
    )
    assert resp.status_code == 200
    assert config.get(1, "primevideo_cookie_header") == "new-cookie=2"


# ── Body validation ───────────────────────────────────────────────────────────


def test_missing_cookie_header_field_is_400(app_client):
    client, tokens = app_client
    resp = client.post("/api/pat/primevideo-cookie", json={}, headers=_auth(tokens["rw1"]))
    assert resp.status_code == 400


def test_blank_cookie_header_is_400(app_client):
    client, tokens = app_client
    resp = client.post(
        "/api/pat/primevideo-cookie", json={"cookie_header": "   "},
        headers=_auth(tokens["rw1"]),
    )
    assert resp.status_code == 400


# ── CSRF exemption only fires for an actually-valid PAT ──────────────────────


def test_garbage_authorization_header_does_not_bypass_csrf(app_client):
    """_csrf_protect runs as an app-level dependency, before any route body
    (including auth checks) — so an unauthenticated POST with no real CSRF
    token normally gets a 403 regardless of what the route itself would have
    done. Issue #390 added a PAT-based exemption to that check for an
    *actually-valid* token; this confirms a bogus/unresolvable Authorization
    header can't be used to slip through that exemption, which would defeat
    the whole point of it. skip_csrf_autoinject=True drives the real,
    undoctored CSRF flow instead of conftest.py's normal auto-injected token
    (see tests/test_csrf_protection.py, the existing precedent for testing
    real CSRF behavior in this repo)."""
    client, _ = app_client
    resp = client.request(
        "POST", "/api/collections",
        json={"name": "should not be created", "filters": {}},
        headers={"Authorization": "Bearer totally-not-a-real-token"},
        skip_csrf_autoinject=True,
    )
    assert resp.status_code == 403
