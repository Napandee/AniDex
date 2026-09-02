"""
Coverage for issue #230 — admin "login as user" impersonation for support
debugging.

Verified against a real Postgres (same throwaway-instance pattern as
tests/test_sessions.py and tests/test_admin_instance_health.py): this exercises
the real `sessions` table shape from schema.sql (migration 027's
impersonated_by/impersonation_expires_at columns), not a hand-duplicated subset
of it, and drives the real FastAPI routes via TestClient for the second half of
this file — not internal functions directly.

Two failure modes get dedicated coverage per the issue's own emphasis, not just
the happy path:
  1. Can a non-admin trigger this? (start route, and the generic write-audit
     middleware never firing for an ordinary non-impersonating session.)
  2. Does impersonation actually expire — both via the explicit Stop button and
     via silent time-box expiry, with the acceptance criterion that expiry must
     return the admin cleanly to their own session rather than leaving anyone
     stuck or logged out.
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

from app import sessions  # noqa: E402  (needs sys.path insert above)


@pytest.fixture()
def two_users(pg_conn):
    """user 1 = admin, user 2 = an ordinary active user. Truncated fresh for
    every test so rows never leak between tests."""
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE sessions, users, admin_audit_log RESTART IDENTITY CASCADE")
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active, is_admin) "
            "VALUES (1, 'local', 'admin@example.com', 'admin@example.com', %s, true, true), "
            "(2, 'local', 'user@example.com', 'user@example.com', %s, true, false)",
            ("x", "x"),
        )
    return (1, 2)


# ── app/sessions.py — direct unit coverage ──────────────────────────────────


def test_start_impersonation_session_sets_impersonation_fields(two_users):
    admin_id, target_id = two_users
    token = sessions.start_impersonation_session(admin_id, target_id, "TestAgent/1.0", "1.2.3.4")

    assert sessions.resolve_session(token) == target_id
    ctx = sessions.get_impersonation_context(token)
    assert ctx is not None
    assert ctx["admin_user_id"] == admin_id
    assert ctx["expires_at"] > datetime.now(timezone.utc)


def test_get_impersonation_context_is_none_for_an_ordinary_session(two_users):
    admin_id, target_id = two_users
    token = sessions.create_session(target_id, "TestAgent/1.0", "1.2.3.4")
    assert sessions.get_impersonation_context(token) is None


def test_resolve_session_auto_revokes_an_expired_impersonation_session(pg_conn, two_users):
    """Direct unit coverage for 'does impersonation actually expire': backdate
    impersonation_expires_at (can't wait real minutes in a test) and confirm
    resolve_session() both refuses to resolve it AND actually revokes the row,
    rather than merely filtering it out for this one call."""
    admin_id, target_id = two_users
    token = sessions.start_impersonation_session(admin_id, target_id, "TestAgent/1.0", "1.2.3.4")

    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE sessions SET impersonation_expires_at = %s WHERE session_token_hash = %s",
            (past, sessions.hash_token(token)),
        )

    assert sessions.resolve_session(token) is None
    assert sessions.get_impersonation_context(token) is None

    with pg_conn.cursor() as cur:
        cur.execute("SELECT revoked_at FROM sessions WHERE session_token_hash = %s", (sessions.hash_token(token),))
        row = cur.fetchone()
    assert row[0] is not None, "expired impersonation session must be revoked, not just filtered"


def test_resolve_session_still_works_normally_for_a_live_impersonation(two_users):
    """A live (not-yet-expired) impersonation session resolves exactly like any
    other session — the extra impersonation columns don't interfere with the
    ordinary happy path."""
    admin_id, target_id = two_users
    token = sessions.start_impersonation_session(admin_id, target_id, "TestAgent/1.0", "1.2.3.4")
    assert sessions.resolve_session(token) == target_id
    assert sessions.resolve_session(token) == target_id  # idempotent


# ── Full-app coverage via TestClient ────────────────────────────────────────


@pytest.fixture()
def app_client(pg_conn, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    import app.main as m
    from fastapi.testclient import TestClient

    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE sessions, users, admin_audit_log RESTART IDENTITY CASCADE")

    return TestClient(m.app), m


def _seed_users(m):
    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active, is_admin) "
        "VALUES (1, 'local', 'admin@example.com', 'admin@example.com', %s, true, true), "
        "(2, 'local', 'user@example.com', 'user@example.com', %s, true, false), "
        "(3, 'local', 'other@example.com', 'other@example.com', %s, true, false)",
        (
            m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),
            m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),
            m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),
        ),
    )


def _login(client, email):
    resp = client.post(
        "/auth/login", data={"email": email, "password": "password123"}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    return resp


# ── "Can a non-admin trigger this?" ─────────────────────────────────────────


def test_non_admin_cannot_start_impersonation(app_client):
    client, m = app_client
    _seed_users(m)
    _login(client, "user@example.com")  # user 2, not an admin

    resp = client.post("/admin/users/3/impersonate", follow_redirects=False)
    assert resp.status_code == 403

    # No session was ever created for user 3, and nothing was logged.
    rows = m.db.fetchall("SELECT * FROM sessions WHERE user_id = 3")
    assert rows == []
    audit = m.db.fetchall("SELECT * FROM admin_audit_log")
    assert audit == []


def test_logged_out_caller_cannot_start_impersonation(app_client):
    client, m = app_client
    _seed_users(m)

    resp = client.post("/admin/users/2/impersonate", follow_redirects=False)
    assert resp.status_code == 303
    assert "/auth/login" in resp.headers["location"]


def test_non_admin_cannot_stop_an_impersonation_they_are_not_in(app_client):
    """The Stop route is deliberately not gated by _require_admin (it has to work
    for the non-admin target's own session while impersonated) — confirm that
    doesn't accidentally let an unrelated ordinary session do anything: with no
    impersonator_sid present, it's a harmless no-op, never a way to hijack
    someone else's session."""
    client, m = app_client
    _seed_users(m)
    _login(client, "user@example.com")
    before = m.db.fetchone("SELECT revoked_at FROM sessions WHERE user_id = 2")

    resp = client.post("/admin/impersonate/stop", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    # Nothing was revoked and the caller is still logged in as themselves —
    # a no-op, not a session hijack or an accidental logout.
    after = m.db.fetchone("SELECT revoked_at FROM sessions WHERE user_id = 2")
    assert before["revoked_at"] is None
    assert after["revoked_at"] is None
    settings_resp = client.get("/settings")
    assert settings_resp.status_code == 200
    assert m.db.fetchall("SELECT * FROM admin_audit_log") == []


# ── Happy path: start, banner, audit log ────────────────────────────────────


def test_admin_can_start_impersonation_and_sees_target_as_nav_user(app_client):
    client, m = app_client
    _seed_users(m)
    _login(client, "admin@example.com")

    resp = client.post("/admin/users/2/impersonate", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    home = client.get("/")
    assert home.status_code == 200
    # The persistent banner is present and names both identities. Matched with
    # the leading markup, not a bare substring — base.html's own cache-bust
    # comment mentions "impersonation-banner" in prose on every page.
    assert '<div class="impersonation-banner"' in home.text
    assert "user@example.com" in home.text
    assert "admin@example.com" in home.text

    audit = m.db.fetchall("SELECT action, admin_user_id, target_user_id FROM admin_audit_log")
    assert len(audit) == 1
    assert audit[0]["action"] == "impersonation_started"
    assert audit[0]["admin_user_id"] == 1
    assert audit[0]["target_user_id"] == 2


def test_impersonation_session_has_exactly_the_targets_own_privileges(app_client):
    """The impersonated session must never grant more than the target's own
    privileges — an ordinary user's impersonation session can't reach /admin."""
    client, m = app_client
    _seed_users(m)
    _login(client, "admin@example.com")
    client.post("/admin/users/2/impersonate", follow_redirects=False)

    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 403


def test_cannot_impersonate_self(app_client):
    client, m = app_client
    _seed_users(m)
    _login(client, "admin@example.com")

    resp = client.post("/admin/users/1/impersonate", follow_redirects=False)
    assert resp.status_code == 400


def test_cannot_impersonate_a_deactivated_user(app_client):
    client, m = app_client
    _seed_users(m)
    m.db.execute("UPDATE users SET is_active = false WHERE id = 2")
    _login(client, "admin@example.com")

    resp = client.post("/admin/users/2/impersonate", follow_redirects=False)
    assert resp.status_code == 400
    assert m.db.fetchall("SELECT * FROM sessions WHERE user_id = 2") == []


def test_cannot_stack_a_second_impersonation(app_client):
    """While impersonating a non-admin target, the in-session identity IS that
    target (no admin privileges), so a second /impersonate attempt is refused by
    the ordinary _require_admin gate itself (403) before ever reaching the
    explicit anti-stacking check in admin_start_impersonation — belt-and-braces:
    the explicit impersonator_sid check in that route still matters for the
    admin-impersonates-another-admin case, where _require_admin alone would
    pass."""
    client, m = app_client
    _seed_users(m)
    _login(client, "admin@example.com")
    client.post("/admin/users/2/impersonate", follow_redirects=False)

    resp = client.post("/admin/users/3/impersonate", follow_redirects=False)
    assert resp.status_code == 403
    # No session was ever created for user 3 — still only the one impersonation
    # session (for user 2) exists.
    assert m.db.fetchall("SELECT id FROM sessions WHERE user_id = 3") == []
    imp_sessions = m.db.fetchall("SELECT user_id FROM sessions WHERE impersonated_by IS NOT NULL")
    assert [r["user_id"] for r in imp_sessions] == [2]


def test_cannot_stack_a_second_impersonation_of_an_admin_target(app_client):
    """The explicit impersonator_sid guard in admin_start_impersonation, not
    _require_admin, is what blocks stacking when the target being impersonated
    is ITSELF an admin (so _require_admin alone would otherwise pass)."""
    client, m = app_client
    _seed_users(m)
    m.db.execute("UPDATE users SET is_admin = true WHERE id = 3")
    _login(client, "admin@example.com")
    client.post("/admin/users/3/impersonate", follow_redirects=False)  # target 3 is an admin

    resp = client.post("/admin/users/2/impersonate", follow_redirects=False)
    assert resp.status_code == 400
    assert m.db.fetchall("SELECT id FROM sessions WHERE user_id = 2") == []


# ── Stop / expiry — "no risk of getting stuck" ──────────────────────────────


def test_stop_returns_cleanly_to_the_admins_own_session(app_client):
    client, m = app_client
    _seed_users(m)
    _login(client, "admin@example.com")
    client.post("/admin/users/2/impersonate", follow_redirects=False)

    resp = client.post("/admin/impersonate/stop", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"

    # Back to the admin's own identity, and /admin is reachable again — the
    # clearest proof of "returns cleanly to the admin's own session", since
    # nav_user.email is only ever rendered inside the banner itself.
    admin_page = client.get("/admin")
    assert admin_page.status_code == 200
    home = client.get("/")
    assert '<div class="impersonation-banner"' not in home.text

    audit = m.db.fetchall(
        "SELECT action, admin_user_id, target_user_id FROM admin_audit_log ORDER BY id"
    )
    assert [a["action"] for a in audit] == ["impersonation_started", "impersonation_ended"]
    assert audit[1]["admin_user_id"] == 1
    assert audit[1]["target_user_id"] == 2


def test_impersonation_session_row_is_actually_revoked_on_stop(app_client):
    """Defense in depth: Stop doesn't just swap the cookie back, it revokes the
    impersonation session row server-side too, so a captured/leaked impersonation
    cookie can't be replayed after the admin has explicitly ended it."""
    client, m = app_client
    _seed_users(m)
    _login(client, "admin@example.com")
    client.post("/admin/users/2/impersonate", follow_redirects=False)

    imp_row = m.db.fetchone(
        "SELECT id, revoked_at FROM sessions WHERE user_id = 2 AND impersonated_by IS NOT NULL"
    )
    assert imp_row is not None
    assert imp_row["revoked_at"] is None  # not yet revoked — the raw token isn't
    # recoverable from the DB anymore (only its hash is stored), so liveness is
    # checked directly via revoked_at rather than round-tripping through
    # resolve_session() with a token this test doesn't have.

    client.post("/admin/impersonate/stop", follow_redirects=False)

    still_row = m.db.fetchone("SELECT revoked_at FROM sessions WHERE id = %s", (imp_row["id"],))
    assert still_row["revoked_at"] is not None


def test_expired_impersonation_falls_back_to_the_admins_session_automatically(app_client):
    """The core 'does impersonation actually expire' acceptance criterion: once
    impersonation_expires_at has passed, the very next request must NOT still be
    acting as the target, and per #230's 'no risk of getting stuck' criterion it
    should transparently fall back to the admin's own session rather than just
    logging everyone out."""
    client, m = app_client
    _seed_users(m)
    _login(client, "admin@example.com")
    client.post("/admin/users/2/impersonate", follow_redirects=False)

    # Confirm we really are impersonating before backdating.
    still_impersonating = client.get("/")
    assert '<div class="impersonation-banner"' in still_impersonating.text

    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    m.db.execute(
        "UPDATE sessions SET impersonation_expires_at = %s "
        "WHERE user_id = 2 AND impersonated_by IS NOT NULL",
        (past,),
    )

    after_expiry = client.get("/")
    assert after_expiry.status_code == 200
    assert '<div class="impersonation-banner"' not in after_expiry.text
    # Back to being the admin — /admin is reachable again without needing to
    # log back in, proving the fallback restored the admin's own session
    # rather than merely dropping the impersonation.
    admin_page = client.get("/admin")
    assert admin_page.status_code == 200


def test_expired_impersonation_with_no_admin_fallback_logs_out_cleanly(app_client):
    """If the admin's own session is ALSO gone by the time impersonation expires
    (e.g. they logged out elsewhere), this must degrade to an ordinary logged-out
    state, never an error or a stuck session."""
    client, m = app_client
    _seed_users(m)
    _login(client, "admin@example.com")
    client.post("/admin/users/2/impersonate", follow_redirects=False)

    # Revoke the admin's own fallback session directly (simulating it having
    # died some other way) and expire the impersonation session.
    m.db.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = 1")
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    m.db.execute(
        "UPDATE sessions SET impersonation_expires_at = %s "
        "WHERE user_id = 2 AND impersonated_by IS NOT NULL",
        (past,),
    )

    resp = client.get("/settings", follow_redirects=False)
    assert resp.status_code == 303
    assert "/auth/login" in resp.headers["location"]


# ── Write-action audit trail while impersonating ────────────────────────────


def test_write_action_while_impersonating_is_logged(app_client):
    client, m = app_client
    _seed_users(m)
    _login(client, "admin@example.com")
    client.post("/admin/users/2/impersonate", follow_redirects=False)

    resp = client.post(
        "/settings/display",
        data={"timezone": "Europe/London", "language": "en", "theme": "dark"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    audit = m.db.fetchall(
        "SELECT action, admin_user_id, target_user_id, detail FROM admin_audit_log "
        "WHERE action = 'impersonation_action'"
    )
    assert len(audit) == 1
    assert audit[0]["admin_user_id"] == 1
    assert audit[0]["target_user_id"] == 2
    assert "/settings/display" in audit[0]["detail"]


def test_read_only_requests_while_impersonating_are_not_logged_as_actions(app_client):
    client, m = app_client
    _seed_users(m)
    _login(client, "admin@example.com")
    client.post("/admin/users/2/impersonate", follow_redirects=False)

    client.get("/")
    client.get("/settings")

    audit = m.db.fetchall(
        "SELECT action FROM admin_audit_log WHERE action = 'impersonation_action'"
    )
    assert audit == []


def test_normal_non_impersonating_writes_are_not_logged_as_impersonation_actions(app_client):
    """The generic write-audit middleware must stay completely silent for an
    ordinary (non-impersonating) session — it should never fire just because
    *an* admin happens to be logged in normally."""
    client, m = app_client
    _seed_users(m)
    _login(client, "admin@example.com")

    resp = client.post(
        "/settings/display",
        data={"timezone": "Europe/London", "language": "en", "theme": "dark"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    audit = m.db.fetchall(
        "SELECT action FROM admin_audit_log WHERE action = 'impersonation_action'"
    )
    assert audit == []
