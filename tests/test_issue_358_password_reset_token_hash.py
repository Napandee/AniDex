"""
Coverage for issue #358 — password_resets.token used to store the raw opaque reset
token in plaintext (the primary key itself), unlike sessions.session_token_hash
(issue #311/migration 030), which closed the identical gap for session tokens. A
DB-read-only leak within the token's 1-hour window was a direct account takeover.

Migration 033 renames the column to token_hash and stores SHA256(token) via
app.sessions.hash_token() — the same hashing convention sessions already use.

Verified against a real Postgres and a real FastAPI TestClient driving the actual
HTTP routes end to end, same pattern as tests/test_totp_2fa.py.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


_next_user_id = [2000]


def _make_local_user(pg_conn, app_module, email=None, is_admin=False):
    _next_user_id[0] += 1
    uid = _next_user_id[0]
    email = email or f"user{uid}@example.com"
    password_hash = app_module.bcrypt.hashpw(b"correct horse battery staple", app_module.bcrypt.gensalt()).decode(
        "utf-8"
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_admin) "
            "VALUES (%s, 'local', %s, %s, %s, %s)",
            (uid, email, email, password_hash, is_admin),
        )
    return uid, email


@pytest.fixture()
def admin_user(pg_conn, app_module):
    return _make_local_user(pg_conn, app_module, is_admin=True)


@pytest.fixture()
def target_user(pg_conn, app_module):
    return _make_local_user(pg_conn, app_module)


def _login_as_admin(client, email):
    return client.post("/auth/login", data={"email": email, "password": "correct horse battery staple"})


def _generate_reset_link(client, pg_conn, admin_user, target_user):
    """Drives the real admin_reset_password route end to end and extracts the raw
    token from the rendered reset URL — same as an admin copy-pasting the link."""
    admin_id, admin_email = admin_user
    target_id, _ = target_user
    _login_as_admin(client, admin_email)

    resp = client.post(f"/admin/users/{target_id}/reset-password")
    assert resp.status_code == 200
    m = __import__("re").search(r"/auth/reset-password/([\w-]+)", resp.text)
    assert m, "expected the admin response to render a reset URL containing the raw token"
    return m.group(1)


# ── 1. The raw token is never stored at rest ────────────────────────────────────────

def test_reset_token_stored_hashed_not_raw(client, pg_conn, app_module, admin_user, target_user):
    token = _generate_reset_link(client, pg_conn, admin_user, target_user)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT token_hash FROM password_resets WHERE user_id = %s", (target_user[0],))
        row = cur.fetchone()

    assert row is not None
    stored = row[0]
    assert stored != token, "the raw token must never be stored at rest"
    assert stored == app_module.sessions.hash_token(token)


# ── 2. A freshly generated token is usable end to end ───────────────────────────────

def test_reset_token_round_trip_succeeds(client, pg_conn, app_module, admin_user, target_user):
    token = _generate_reset_link(client, pg_conn, admin_user, target_user)
    client.get("/auth/logout", follow_redirects=False)

    page = client.get(f"/auth/reset-password/{token}")
    assert page.status_code == 200

    resp = client.post(f"/auth/reset-password/{token}", data={"password": "a brand new password 123"})
    assert resp.status_code in (200, 303)
    assert resp.headers.get("location", "/auth/login") == "/auth/login" if resp.status_code == 303 else True

    with pg_conn.cursor() as cur:
        cur.execute("SELECT used_at FROM password_resets WHERE user_id = %s", (target_user[0],))
        used_at = cur.fetchone()[0]
    assert used_at is not None

    # The now-updated password actually works.
    login_resp = client.post(
        "/auth/login",
        data={"email": target_user[1], "password": "a brand new password 123"},
        follow_redirects=False,
    )
    assert login_resp.status_code == 303
    assert login_resp.headers["location"] == "/"


# ── 3. An unknown/garbage token is rejected ─────────────────────────────────────────

def test_unknown_token_rejected(client, pg_conn, app_module, admin_user, target_user):
    _generate_reset_link(client, pg_conn, admin_user, target_user)  # real row exists in the table

    resp = client.get("/auth/reset-password/not-a-real-token-at-all")
    assert resp.status_code == 400


# ── 4. A used token can't be reused ─────────────────────────────────────────────────

def test_used_token_rejected_on_reuse(client, pg_conn, app_module, admin_user, target_user):
    token = _generate_reset_link(client, pg_conn, admin_user, target_user)
    client.get("/auth/logout", follow_redirects=False)

    first = client.post(f"/auth/reset-password/{token}", data={"password": "first new password 123"})
    assert first.status_code in (200, 303)

    second = client.post(f"/auth/reset-password/{token}", data={"password": "second new password 123"})
    assert second.status_code == 400


# ── 5. An expired token is rejected ─────────────────────────────────────────────────

def test_expired_token_rejected(pg_conn, app_module, target_user):
    token = "expired-token-for-test-358"
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO password_resets (token_hash, user_id, expires_at) "
            "VALUES (%s, %s, now() - interval '1 hour')",
            (app_module.sessions.hash_token(token), target_user[0]),
        )

    assert app_module._valid_reset_token(token) is None
