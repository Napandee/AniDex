"""
Coverage for issue #82 — server-side session store + Settings "active sessions"
view/revoke.

Before this, the app's ENTIRE session lived in a signed cookie (Starlette's
SessionMiddleware, payload `{"user_id": N}`) with nothing server-side to list or
revoke. This adds a real `sessions` table (migration 014 / schema.sql) and changes
the cookie payload to an opaque `{"sid": "<token>"}`, resolved against that table on
every request — see app/sessions.py and app/main.py's get_current_user /
_start_session / _end_session.

Verified against a real Postgres, same pattern as tests/test_admin_instance_health.py
and tests/test_recommendation_snooze.py: this exercises the real `sessions` table
shape from schema.sql, not a hand-duplicated subset of it, plus (in the second half
of this file) the actual FastAPI routes via TestClient rather than calling internal
functions directly — login, Settings, and the revoke endpoint are what a real
browser session goes through.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no
Postgres running.

Covers the acceptance criteria from issue #82:
  1. A new session row is created on login.
  2. Settings lists only the current user's own active sessions (never another
     user's).
  3. Revoking a session actually invalidates it — a subsequent request bearing that
     session's cookie fails auth.
  4. Revoking the CURRENT session logs the caller out immediately.
Plus the rollout concern called out in the PR: a pre-#82 cookie (old shape, no "sid"
key at all) is treated as a clean logout, not a crash.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest
from itsdangerous import URLSafeTimedSerializer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import sessions  # noqa: E402  (needs sys.path insert above)

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
def two_users(pg_conn):
    """A fresh pair of users (ids 1 and 2), with `sessions`/`users` truncated first
    so rows from one test never leak into the next."""
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE sessions, users RESTART IDENTITY CASCADE")
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
            "VALUES (1, 'local', 'a@example.com', 'a@example.com', %s, true), "
            "(2, 'local', 'b@example.com', 'b@example.com', %s, true)",
            ("x", "x"),
        )
    return (1, 2)


# ── app/sessions.py — direct unit coverage ─────────────────────────────────────


def test_create_session_inserts_a_row(two_users):
    user_id, _ = two_users
    token = sessions.create_session(user_id, "TestAgent/1.0", "1.2.3.4")
    active = sessions.list_active_sessions(user_id, current_token=None)
    assert len(active) == 1
    assert active[0]["user_agent"] == "TestAgent/1.0"
    assert active[0]["ip_address"] == "1.2.3.4"
    assert token  # a real opaque token was returned


def test_create_session_truncates_an_oversized_ip_address(two_users):
    """Code-review finding: ip_address comes straight from the X-Forwarded-For
    header (attacker-controllable — no comma at all makes the "first element" the
    entire, arbitrarily long string), and must be capped the same way user_agent
    already was."""
    user_id, _ = two_users
    huge_ip = "1" * 5000
    sessions.create_session(user_id, "TestAgent/1.0", huge_ip)
    active = sessions.list_active_sessions(user_id, current_token=None)
    assert len(active[0]["ip_address"]) == sessions._IP_ADDRESS_MAX_LEN


def test_create_session_handles_missing_ip_address(two_users):
    user_id, _ = two_users
    sessions.create_session(user_id, "TestAgent/1.0", None)
    active = sessions.list_active_sessions(user_id, current_token=None)
    assert active[0]["ip_address"] is None


def test_resolve_session_returns_owning_user_id(two_users):
    user_id, _ = two_users
    token = sessions.create_session(user_id, "TestAgent/1.0", "1.2.3.4")
    assert sessions.resolve_session(token) == user_id


def test_resolve_session_returns_none_for_unknown_token(two_users):
    assert sessions.resolve_session("not-a-real-token") is None


def test_resolve_session_does_not_write_last_seen_at_when_already_fresh(two_users, pg_conn):
    """Code-review finding: resolve_session() runs on every authenticated request,
    so it must not turn that into an unconditional write on every single call —
    only when last_seen_at is stale by more than _LAST_SEEN_THROTTLE_MINUTES."""
    user_id, _ = two_users
    token = sessions.create_session(user_id, "TestAgent/1.0", "1.2.3.4")
    with pg_conn.cursor() as cur:
        cur.execute("SELECT last_seen_at FROM sessions WHERE session_token = %s", (token,))
        before = cur.fetchone()[0]

    sessions.resolve_session(token)  # freshly created — well within the throttle window

    with pg_conn.cursor() as cur:
        cur.execute("SELECT last_seen_at FROM sessions WHERE session_token = %s", (token,))
        after = cur.fetchone()[0]
    assert after == before


def test_resolve_session_writes_last_seen_at_once_stale(two_users, pg_conn):
    user_id, _ = two_users
    token = sessions.create_session(user_id, "TestAgent/1.0", "1.2.3.4")
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=sessions._LAST_SEEN_THROTTLE_MINUTES + 1)
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE sessions SET last_seen_at = %s WHERE session_token = %s",
            (stale_time, token),
        )

    assert sessions.resolve_session(token) == user_id

    with pg_conn.cursor() as cur:
        cur.execute("SELECT last_seen_at FROM sessions WHERE session_token = %s", (token,))
        after = cur.fetchone()[0]
    assert after > stale_time


def test_revoked_session_no_longer_resolves(two_users):
    """Acceptance criterion: revoking a session actually invalidates it."""
    user_id, _ = two_users
    token = sessions.create_session(user_id, "TestAgent/1.0", "1.2.3.4")
    assert sessions.resolve_session(token) == user_id

    sessions.revoke_session_by_token(token)

    assert sessions.resolve_session(token) is None


def test_revoke_session_is_scoped_to_owning_user(two_users):
    """A user can't revoke another user's session, even by guessing/incrementing
    an id — revoke_session's UPDATE is scoped to (id, user_id)."""
    user1, user2 = two_users
    token = sessions.create_session(user1, "TestAgent/1.0", "1.2.3.4")
    session_row = sessions.list_active_sessions(user1, current_token=None)[0]

    # user2 tries to revoke user1's session id.
    result = sessions.revoke_session(session_row["id"], user2)

    assert result is None  # no matching row for (that id, user2) -> silent no-op
    assert sessions.resolve_session(token) == user1  # still fully active


def test_list_active_sessions_only_returns_that_users_own_rows(two_users):
    """Settings must show only the current user's own sessions, never another
    user's — this is the exact acceptance criterion from issue #82."""
    user1, user2 = two_users
    sessions.create_session(user1, "Agent-1", "1.1.1.1")
    sessions.create_session(user2, "Agent-2", "2.2.2.2")

    user1_sessions = sessions.list_active_sessions(user1, current_token=None)
    user2_sessions = sessions.list_active_sessions(user2, current_token=None)

    assert len(user1_sessions) == 1
    assert len(user2_sessions) == 1
    assert user1_sessions[0]["id"] != user2_sessions[0]["id"]


def test_list_active_sessions_excludes_revoked_and_never_leaks_the_token(two_users):
    user_id, _ = two_users
    token = sessions.create_session(user_id, "Agent-1", "1.1.1.1")
    sessions.revoke_session_by_token(token)

    active = sessions.list_active_sessions(user_id, current_token=token)

    assert active == []


def test_list_active_sessions_flags_the_current_session(two_users):
    user_id, _ = two_users
    token_a = sessions.create_session(user_id, "Agent-A", "1.1.1.1")
    token_b = sessions.create_session(user_id, "Agent-B", "2.2.2.2")

    active = sessions.list_active_sessions(user_id, current_token=token_b)

    assert len(active) == 2
    current_rows = [row for row in active if row["is_current"]]
    assert len(current_rows) == 1
    # No raw token in the returned dicts at all — is_current is the only thing
    # exposed about the caller's own session.
    for row in active:
        assert "session_token" not in row


def test_revoke_all_sessions_revokes_every_active_row_for_that_user(two_users):
    user1, user2 = two_users
    sessions.create_session(user1, "Agent-1", "1.1.1.1")
    sessions.create_session(user1, "Agent-2", "1.1.1.2")
    token_other = sessions.create_session(user2, "Agent-3", "2.2.2.2")

    sessions.revoke_all_sessions(user1)

    assert sessions.list_active_sessions(user1, current_token=None) == []
    # Untouched — revoke_all_sessions is scoped to the target user only.
    assert sessions.resolve_session(token_other) == user2


def test_describe_device_distinguishes_common_browsers_and_platforms():
    iphone_safari = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )
    windows_chrome = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    windows_edge = windows_chrome + " Edg/120.0.0.0"

    assert "iPhone" in sessions.describe_device(iphone_safari)
    assert "Safari" in sessions.describe_device(iphone_safari)
    assert "Chrome" in sessions.describe_device(windows_chrome)
    assert "Windows" in sessions.describe_device(windows_chrome)
    assert "Edge" in sessions.describe_device(windows_edge)
    assert sessions.describe_device(None) == "Unknown device"


def test_schema_has_a_plain_user_id_index_for_the_cleanup_delete(pg_conn):
    """Code-review finding: idx_sessions_user_active is a *partial* index (WHERE
    revoked_at IS NULL), which the cleanup DELETE in create_session() can't use —
    it specifically targets revoked_at IS NOT NULL rows too. Verified with EXPLAIN
    against a seeded table that a plain (non-partial) index on user_id fixes this
    (Bitmap Heap Scan on ~40 of that user's own rows vs a Seq Scan over the whole
    table). This just guards against that index silently disappearing later."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'sessions' AND indexname = 'idx_sessions_user_id'"
        )
        row = cur.fetchone()
    assert row is not None, "idx_sessions_user_id is missing from schema.sql"
    assert "WHERE" not in row[0].upper(), "idx_sessions_user_id must stay a plain (non-partial) index"


# ── Full-app coverage via TestClient — the real /auth/login, /settings, and
#    /settings/sessions/{id}/revoke routes, not internal functions directly ────


@pytest.fixture()
def app_client(pg_conn, monkeypatch):
    """A TestClient wired to the same throwaway Postgres as pg_conn, importing
    app.main lazily (after DATABASE_URL/SESSION_SECRET_KEY are set) same as
    tests/test_admin_instance_health.py's `instance_health` fixture."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    import app.main as m
    from fastapi.testclient import TestClient

    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE sessions, users RESTART IDENTITY CASCADE")

    return TestClient(m.app), m


def test_login_creates_a_session_row(app_client):
    client, m = app_client
    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
        "VALUES (1, 'local', 'a@example.com', 'a@example.com', %s, true)",
        (m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),),
    )

    assert m.db.fetchall("SELECT * FROM sessions") == []

    resp = client.post(
        "/auth/login", data={"email": "a@example.com", "password": "password123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    rows = m.db.fetchall("SELECT * FROM sessions")
    assert len(rows) == 1
    assert rows[0]["user_id"] == 1
    assert rows[0]["revoked_at"] is None


def test_settings_page_lists_only_the_logged_in_users_own_session(app_client):
    client, m = app_client
    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
        "VALUES (1, 'local', 'a@example.com', 'a@example.com', %s, true), "
        "(2, 'local', 'b@example.com', 'b@example.com', %s, true)",
        (
            m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),
            m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),
        ),
    )
    # Another user's session, with a distinctive user-agent that must never appear
    # on user 1's Settings page.
    m.sessions.create_session(2, "OtherUsersDevice/9.9", "9.9.9.9")

    client.post("/auth/login", data={"email": "a@example.com", "password": "password123"})

    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Active sessions" in resp.text
    assert "OtherUsersDevice" not in resp.text


def test_revoking_another_session_leaves_current_login_intact(app_client):
    client, m = app_client
    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
        "VALUES (1, 'local', 'a@example.com', 'a@example.com', %s, true)",
        (m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),),
    )
    # A second, non-current session for the same user (e.g. another device).
    other_token = m.sessions.create_session(1, "OtherDevice/1.0", "5.5.5.5")

    client.post("/auth/login", data={"email": "a@example.com", "password": "password123"})
    other_row = m.db.fetchone("SELECT id FROM sessions WHERE session_token = %s", (other_token,))

    resp = client.post(f"/settings/sessions/{other_row['id']}/revoke", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?saved=session_revoked"

    # The revoked (other) session is dead...
    assert m.sessions.resolve_session(other_token) is None
    # ...but the caller's OWN current login still works.
    still_in = client.get("/settings")
    assert still_in.status_code == 200


def test_revoking_an_already_revoked_session_does_not_show_a_false_success_message(app_client):
    """Code-review finding: double-clicking Revoke, or revoking the same session
    from two open tabs, must not show a false-positive "revoked" confirmation on
    the second, no-op request."""
    client, m = app_client
    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
        "VALUES (1, 'local', 'a@example.com', 'a@example.com', %s, true)",
        (m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),),
    )
    other_token = m.sessions.create_session(1, "OtherDevice/1.0", "5.5.5.5")
    client.post("/auth/login", data={"email": "a@example.com", "password": "password123"})
    other_row = m.db.fetchone("SELECT id FROM sessions WHERE session_token = %s", (other_token,))

    first = client.post(f"/settings/sessions/{other_row['id']}/revoke", follow_redirects=False)
    assert first.headers["location"] == "/settings?saved=session_revoked"

    second = client.post(f"/settings/sessions/{other_row['id']}/revoke", follow_redirects=False)
    assert second.status_code == 303
    assert second.headers["location"] == "/settings"  # no saved= param — nothing actually happened


def test_revoking_an_unknown_session_id_is_a_silent_no_op(app_client):
    client, m = app_client
    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
        "VALUES (1, 'local', 'a@example.com', 'a@example.com', %s, true)",
        (m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),),
    )
    client.post("/auth/login", data={"email": "a@example.com", "password": "password123"})

    resp = client.post("/settings/sessions/999999/revoke", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings"


def test_revoking_current_session_logs_the_user_out(app_client):
    """Acceptance criterion: revoking the current session logs the user out."""
    client, m = app_client
    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
        "VALUES (1, 'local', 'a@example.com', 'a@example.com', %s, true)",
        (m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),),
    )
    client.post("/auth/login", data={"email": "a@example.com", "password": "password123"})

    current_row = m.db.fetchone("SELECT id FROM sessions WHERE user_id = 1")
    resp = client.post(f"/settings/sessions/{current_row['id']}/revoke", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"

    # A subsequent request with the same (now-dead) cookie is unauthenticated.
    followup = client.get("/settings", follow_redirects=False)
    assert followup.status_code == 303
    assert "/auth/login" in followup.headers["location"]


def test_pre_82_cookie_shape_is_treated_as_a_clean_logout_not_a_crash(app_client):
    """Rollout scenario called out in the PR: every session that existed before
    this shipped has a cookie in the OLD shape ({"user_id": N}, no "sid" key). This
    must fail auth gracefully (redirect to login), never throw."""
    client, m = app_client
    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
        "VALUES (1, 'local', 'a@example.com', 'a@example.com', %s, true)",
        (m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),),
    )
    signer = URLSafeTimedSerializer(m._SESSION_SECRET_KEY, salt="starlette.sessions")
    old_style_cookie = signer.dumps({"user_id": 1})
    client.cookies.set("session", old_style_cookie)

    resp = client.get("/settings", follow_redirects=False)

    assert resp.status_code == 303
    assert "/auth/login" in resp.headers["location"]


def test_deactivating_a_user_revokes_their_active_sessions(app_client):
    client, m = app_client
    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_admin, is_active) "
        "VALUES (1, 'local', 'admin@example.com', 'admin@example.com', %s, true, true), "
        "(2, 'local', 'b@example.com', 'b@example.com', %s, false, true)",
        (
            m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),
            m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),
        ),
    )
    target_token = m.sessions.create_session(2, "TargetDevice/1.0", "3.3.3.3")
    assert m.sessions.resolve_session(target_token) == 2

    client.post("/auth/login", data={"email": "admin@example.com", "password": "password123"})
    resp = client.post("/admin/users/2/deactivate", follow_redirects=False)
    assert resp.status_code == 303

    assert m.sessions.resolve_session(target_token) is None
