"""
Coverage for issue #231 — invite expiry + pending/expired/accepted status
visibility with resend/revoke controls in Admin -> Invites.

Before this, the `invites` table had no expiry concept at all: a row created by
POST /admin/invites (email UNIQUE, ON CONFLICT DO NOTHING) lived forever with no
way to see its real state or reissue/cancel it short of direct database surgery —
the exact "invited limbo" pain point #231's Context section calls out (observed
in Vaultwarden's own invite system). This adds `expires_at`/`revoked_at`
(migration 022 / schema.sql), enforces expiry+revocation at signup time in
`_resolve_or_create_user`, computes a pending/expired/accepted/revoked status for
display in `_invite_status`, and adds POST /admin/invites/{id}/resend and
POST /admin/invites/{id}/revoke.

Verified against a real Postgres, same pattern as tests/test_sessions.py: this
exercises the real `invites` table shape from schema.sql, not a hand-duplicated
subset of it, plus the actual FastAPI routes via TestClient.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no
Postgres running.

Covers the acceptance criteria from issue #231:
  1. Invites expire after a defined window if unused.
  2. Admin -> Invites shows pending/expired/accepted status per invite.
  3. Admin can resend or revoke an outstanding invite.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
        # Fully clean slate each run, applying the real schema.sql — not a hand-kept
        # subset of it — so this test can't silently drift from the actual table shape.
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(SCHEMA_SQL)
    yield conn
    conn.close()


@pytest.fixture()
def app_client(pg_conn, monkeypatch):
    """A TestClient wired to the same throwaway Postgres as pg_conn, importing
    app.main lazily (after DATABASE_URL/SESSION_SECRET_KEY are set) same as
    tests/test_sessions.py's app_client fixture."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    import app.main as m
    from fastapi.testclient import TestClient

    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE invites, sessions, users RESTART IDENTITY CASCADE")

    return TestClient(m.app), m


def _make_admin(m, email="admin@example.com", password="password123"):
    # Deliberately no explicit id= here (unlike tests/test_sessions.py's two_users
    # fixture) — this suite's invites all reference invited_by=1 via raw SQL, and
    # an explicit id=1 insert leaves the `users_id_seq` sequence un-advanced,
    # so the *next* INSERT INTO users without an explicit id (the real signup
    # path, _resolve_or_create_user) collides on the still-unused id=1 primary
    # key. Letting this insert consume the sequence normally keeps the two paths
    # in sync — RESTART IDENTITY in the app_client fixture guarantees this is id=1
    # each test anyway.
    m.db.execute(
        "INSERT INTO users (auth_provider, auth_provider_id, email, password_hash, is_admin, is_active) "
        "VALUES ('local', %s, %s, %s, true, true)",
        (email, email, m.bcrypt.hashpw(password.encode(), m.bcrypt.gensalt()).decode()),
    )


def _login_admin(client, email="admin@example.com", password="password123"):
    resp = client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=False)
    assert resp.status_code == 303


# ── app/main.py's _invite_status — direct unit coverage ─────────────────────


def test_invite_status_pending_for_unexpired_unaccepted_unrevoked(app_client):
    _, m = app_client
    now = datetime.now(timezone.utc)
    invite = {"accepted_at": None, "revoked_at": None, "expires_at": now + timedelta(days=1)}
    assert m._invite_status(invite, now) == "pending"


def test_invite_status_expired_once_past_expires_at(app_client):
    _, m = app_client
    now = datetime.now(timezone.utc)
    invite = {"accepted_at": None, "revoked_at": None, "expires_at": now - timedelta(seconds=1)}
    assert m._invite_status(invite, now) == "expired"


def test_invite_status_revoked_takes_priority_over_expired(app_client):
    _, m = app_client
    now = datetime.now(timezone.utc)
    invite = {"accepted_at": None, "revoked_at": now - timedelta(days=1), "expires_at": now - timedelta(days=2)}
    assert m._invite_status(invite, now) == "revoked"


def test_invite_status_accepted_wins_even_with_a_past_expires_at(app_client):
    """An invite that was accepted before it expired must still read as
    "accepted" — not "expired" just because time has since moved past
    expires_at. Order-of-checks regression guard."""
    _, m = app_client
    now = datetime.now(timezone.utc)
    invite = {
        "accepted_at": now - timedelta(days=10),
        "revoked_at": None,
        "expires_at": now - timedelta(days=3),
    }
    assert m._invite_status(invite, now) == "accepted"


# ── Signup-time enforcement (_resolve_or_create_user) ────────────────────────


def test_signup_succeeds_against_a_pending_unexpired_invite(app_client):
    client, m = app_client
    _make_admin(m)
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at) VALUES (%s, 1, now() + interval '7 days')",
        ("newbie@example.com",),
    )

    resp = client.post(
        "/auth/register",
        data={"email": "newbie@example.com", "password": "password123"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    user = m.db.fetchone("SELECT * FROM users WHERE email = %s", ("newbie@example.com",))
    assert user is not None
    invite = m.db.fetchone("SELECT * FROM invites WHERE email = %s", ("newbie@example.com",))
    assert invite["accepted_at"] is not None


def test_signup_rejected_against_an_expired_invite(app_client):
    """Acceptance criterion: invites expire after a defined window if unused."""
    client, m = app_client
    _make_admin(m)
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at) VALUES (%s, 1, now() - interval '1 hour')",
        ("toolate@example.com",),
    )

    resp = client.post(
        "/auth/register",
        data={"email": "toolate@example.com", "password": "password123"},
        follow_redirects=False,
    )

    assert resp.status_code == 403
    assert m.db.fetchone("SELECT * FROM users WHERE email = %s", ("toolate@example.com",)) is None


def test_signup_rejected_against_a_revoked_invite(app_client):
    client, m = app_client
    _make_admin(m)
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at, revoked_at) "
        "VALUES (%s, 1, now() + interval '7 days', now())",
        ("revokedguy@example.com",),
    )

    resp = client.post(
        "/auth/register",
        data={"email": "revokedguy@example.com", "password": "password123"},
        follow_redirects=False,
    )

    assert resp.status_code == 403
    assert m.db.fetchone("SELECT * FROM users WHERE email = %s", ("revokedguy@example.com",)) is None


def test_new_invite_row_defaults_expires_at_seven_days_out(app_client):
    client, m = app_client
    _make_admin(m)
    _login_admin(client)

    resp = client.post("/admin/invites", data={"email": "fresh@example.com"}, follow_redirects=False)
    assert resp.status_code == 303

    invite = m.db.fetchone("SELECT * FROM invites WHERE email = %s", ("fresh@example.com",))
    assert invite["expires_at"] is not None
    delta = invite["expires_at"] - invite["created_at"]
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)


# ── Admin -> Invites status display ──────────────────────────────────────────


def test_admin_page_shows_pending_expired_accepted_and_revoked_invites(app_client):
    """Acceptance criterion: Admin -> Invites shows pending/expired/accepted
    status per invite."""
    client, m = app_client
    _make_admin(m)
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at, accepted_at) "
        "VALUES (%s, 1, now() + interval '7 days', now())",
        ("accepted@example.com",),
    )
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at) VALUES (%s, 1, now() - interval '1 day')",
        ("expired@example.com",),
    )
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at, revoked_at) "
        "VALUES (%s, 1, now() + interval '7 days', now())",
        ("revoked@example.com",),
    )
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at) VALUES (%s, 1, now() + interval '7 days')",
        ("pending@example.com",),
    )
    _login_admin(client)

    resp = client.get("/admin")

    assert resp.status_code == 200
    body = resp.text
    for email in ("accepted@example.com", "expired@example.com", "revoked@example.com", "pending@example.com"):
        assert email in body


# ── Resend ────────────────────────────────────────────────────────────────


def test_resend_refreshes_expiry_and_clears_revocation(app_client):
    """Acceptance criterion: admin can resend an outstanding invite. Covers the
    "reissue a fresh invite" case for both an expired AND a previously-revoked
    row — resend un-revokes too."""
    client, m = app_client
    _make_admin(m)
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at, revoked_at) "
        "VALUES (%s, 1, now() - interval '1 day', now() - interval '12 hours') RETURNING id",
        ("stale@example.com",),
    )
    invite_id = m.db.fetchone("SELECT id FROM invites WHERE email = %s", ("stale@example.com",))["id"]
    _login_admin(client)

    resp = client.post(f"/admin/invites/{invite_id}/resend", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin?saved=invite_resent"
    invite = m.db.fetchone("SELECT * FROM invites WHERE id = %s", (invite_id,))
    assert invite["revoked_at"] is None
    assert invite["expires_at"] > datetime.now(timezone.utc) + timedelta(days=6)


def test_resend_lets_a_resent_invite_be_used_for_signup(app_client):
    client, m = app_client
    _make_admin(m)
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at) VALUES (%s, 1, now() - interval '1 day')",
        ("comeback@example.com",),
    )
    invite_id = m.db.fetchone("SELECT id FROM invites WHERE email = %s", ("comeback@example.com",))["id"]
    _login_admin(client)
    client.post(f"/admin/invites/{invite_id}/resend", follow_redirects=False)

    logout_client, _ = app_client
    resp = logout_client.post(
        "/auth/register",
        data={"email": "comeback@example.com", "password": "password123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_resend_on_an_accepted_invite_is_rejected(app_client):
    client, m = app_client
    _make_admin(m)
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at, accepted_at) "
        "VALUES (%s, 1, now() + interval '7 days', now())",
        ("used@example.com",),
    )
    invite_id = m.db.fetchone("SELECT id FROM invites WHERE email = %s", ("used@example.com",))["id"]
    _login_admin(client)

    resp = client.post(f"/admin/invites/{invite_id}/resend", follow_redirects=False)

    assert resp.status_code == 404


def test_resend_of_unknown_invite_id_is_rejected(app_client):
    client, m = app_client
    _make_admin(m)
    _login_admin(client)

    resp = client.post("/admin/invites/999999/resend", follow_redirects=False)

    assert resp.status_code == 404


# ── Revoke ────────────────────────────────────────────────────────────────


def test_revoke_invalidates_a_pending_invite(app_client):
    """Acceptance criterion: admin can revoke an outstanding invite."""
    client, m = app_client
    _make_admin(m)
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at) VALUES (%s, 1, now() + interval '7 days')",
        ("cancelme@example.com",),
    )
    invite_id = m.db.fetchone("SELECT id FROM invites WHERE email = %s", ("cancelme@example.com",))["id"]
    _login_admin(client)

    resp = client.post(f"/admin/invites/{invite_id}/revoke", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin?saved=invite_revoked"
    invite = m.db.fetchone("SELECT * FROM invites WHERE id = %s", (invite_id,))
    assert invite["revoked_at"] is not None


def test_revoked_invite_blocks_signup(app_client):
    client, m = app_client
    _make_admin(m)
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at) VALUES (%s, 1, now() + interval '7 days')",
        ("blocked@example.com",),
    )
    invite_id = m.db.fetchone("SELECT id FROM invites WHERE email = %s", ("blocked@example.com",))["id"]
    _login_admin(client)
    client.post(f"/admin/invites/{invite_id}/revoke", follow_redirects=False)

    logout_client, _ = app_client
    resp = logout_client.post(
        "/auth/register",
        data={"email": "blocked@example.com", "password": "password123"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_revoke_on_an_already_accepted_invite_is_rejected(app_client):
    client, m = app_client
    _make_admin(m)
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at, accepted_at) "
        "VALUES (%s, 1, now() + interval '7 days', now())",
        ("alreadyin@example.com",),
    )
    invite_id = m.db.fetchone("SELECT id FROM invites WHERE email = %s", ("alreadyin@example.com",))["id"]
    _login_admin(client)

    resp = client.post(f"/admin/invites/{invite_id}/revoke", follow_redirects=False)

    assert resp.status_code == 404
    invite = m.db.fetchone("SELECT * FROM invites WHERE id = %s", (invite_id,))
    assert invite["revoked_at"] is None


def test_revoking_an_already_revoked_invite_does_not_show_a_false_success_message(app_client):
    """Same double-click/two-tabs guard as tests/test_sessions.py's equivalent
    session-revoke test — the second, no-op revoke must not report success."""
    client, m = app_client
    _make_admin(m)
    m.db.execute(
        "INSERT INTO invites (email, invited_by, expires_at) VALUES (%s, 1, now() + interval '7 days')",
        ("doubleclick@example.com",),
    )
    invite_id = m.db.fetchone("SELECT id FROM invites WHERE email = %s", ("doubleclick@example.com",))["id"]
    _login_admin(client)

    first = client.post(f"/admin/invites/{invite_id}/revoke", follow_redirects=False)
    assert first.headers["location"] == "/admin?saved=invite_revoked"

    second = client.post(f"/admin/invites/{invite_id}/revoke", follow_redirects=False)
    assert second.status_code == 404
