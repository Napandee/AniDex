"""
Coverage for issue #313 — verifying the Google/Discord OAuth login and
account-linking callbacks correctly validate the `state` parameter (the
OAuth-specific CSRF attack: tricking a victim into completing a callback that
links an attacker's own provider identity to the victim's AniDex account —
distinct from general, non-OAuth CSRF, which is tracked separately).

Investigation finding: this app never implements state generation/verification
itself. Every OAuth route (auth_login, auth_callback, settings_link,
auth_link_callback in app/main.py) calls straight into authlib's
StarletteOAuth2App.authorize_redirect() / authorize_access_token() with no
`state=` override — so state handling is entirely authlib's (1.7.2, see
requirements.txt) built-in behavior:

  - authorize_redirect() -> create_authorization_url() ->
    OAuth2Client.create_authorization_url() (authlib/oauth2/client.py)
    generates a fresh random state via authlib.common.security.generate_token()
    whenever the caller doesn't supply one — this app never supplies one, for
    either provider or either flow.
  - StarletteAppMixin.save_authorize_data() (authlib/integrations/
    starlette_client/apps.py) then stores that state, keyed by its own random
    value, in request.session — Starlette's signed-cookie SessionMiddleware,
    registered in app/main.py with SESSION_SECRET_KEY — i.e. genuinely
    session/browser-bound, not just echoed back to the client unchecked.
  - authorize_access_token() looks up `_state_{provider}_{state}` in the
    session for whatever `state` value came back on the callback query
    string (authlib/integrations/starlette_client/integration.py), and
    StarletteOAuth2App._format_state_params() (authlib/integrations/
    base_client/sync_app.py) raises MismatchingStateError the moment that
    lookup comes back empty — covering both a missing state param and one
    that doesn't match what this session actually stored. The state entry is
    also cleared on first use (clear_state_data), so it can't be replayed.

No app code needed to add or change this — confirmed already correct for
both providers and both the login (/auth/login/{provider} ->
/auth/callback/{provider}) and linking (/settings/link/{provider} ->
/auth/link-callback/{provider}) flows, since all four routes share the exact
same authlib call pattern with no provider-specific divergence. This file
adds the regression test the issue asks for anyway, so a future change that
starts passing an explicit/predictable/session-independent `state=` value
(or that adds a custom callback path that skips authlib's default flow)
gets caught rather than silently reopening this gap.

Uses Discord rather than Google as the exercised provider: Discord's
authorize_url/access_token_url are static (see app/main.py's
_ensure_oauth_registered), so redirect-time registration needs no network
call; Google's server_metadata_url requires a live fetch of Google's OIDC
discovery document, which a sandboxed test run can't reach. The state
verification machinery itself is identical for both providers (same
StarletteOAuth2App base class, no provider-specific override anywhere in
this app), so exercising it via Discord is representative of Google's path
too.

Needs a reachable Postgres via DATABASE_URL, same skip-if-unavailable
pattern as tests/test_sessions.py — oauth_configured() reads the
instance_config table (falling back to env vars only when that's empty)
before every OAuth route even starts, so these routes need a real schema
even though this test never actually reaches Discord's servers (every case
here is rejected at the state check, before token exchange).
"""

import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
    """A TestClient wired to the same throwaway Postgres, with Discord OAuth
    "configured" purely via env vars — no real Discord app is ever needed,
    since every case below is rejected at the state-parameter check before
    this ever reaches token exchange. raise_server_exceptions=False so an
    unhandled MismatchingStateError (this app has no OAuthError exception
    handler — confirmed by grep, so it propagates to Starlette's default
    ServerErrorMiddleware) surfaces as a plain 500 response instead of
    raising through the test itself."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("DISCORD_CLIENT_ID", "test-discord-client-id")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "test-discord-client-secret")
    import app.main as m
    from fastapi.testclient import TestClient

    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE sessions, users RESTART IDENTITY CASCADE")

    return TestClient(m.app, raise_server_exceptions=False), m


def _start_login_and_capture_state(client):
    """Drives the real /auth/login/discord route and pulls the actual,
    randomly generated `state` value authlib attached to the redirect
    Location header — the same value it filed away in this client's own
    session cookie via save_authorize_data()."""
    resp = client.get("/auth/login/discord", follow_redirects=False)
    assert resp.status_code in (302, 307)
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert "state" in qs, "authlib did not attach a state param to the redirect — check authlib version/config"
    return qs["state"][0]


# ── Ordinary login callback (/auth/callback/{provider}) ─────────────────────


def test_login_redirect_carries_a_real_state_param(app_client):
    """Sanity check that authlib is actually doing the state-generation half
    of the job (not just something this test assumes)."""
    client, _m = app_client
    state = _start_login_and_capture_state(client)
    assert state  # non-empty, i.e. authlib actually generated one


def test_login_callback_rejects_missing_state(app_client):
    """A login attempt was genuinely started in this session, but the callback
    comes back with no state param at all — must not proceed."""
    client, m = app_client
    _start_login_and_capture_state(client)

    resp = client.get("/auth/callback/discord", params={"code": "fake-code"})

    assert resp.status_code == 500
    # No user should ever get created off a callback that never proved it
    # originated from this session's own login attempt.
    assert m.db.fetchall("SELECT * FROM users") == []


def test_login_callback_rejects_mismatched_state(app_client):
    """The actual attack this issue is about: a state value that doesn't
    match what this session's own login attempt stored (e.g. an attacker
    crafting or replaying a callback URL against a victim's browser)."""
    client, m = app_client
    _start_login_and_capture_state(client)

    resp = client.get(
        "/auth/callback/discord",
        params={"code": "fake-code", "state": "attacker-supplied-state-value"},
    )

    assert resp.status_code == 500
    assert m.db.fetchall("SELECT * FROM users") == []


def test_login_callback_rejects_state_with_no_login_attempt_in_session(app_client):
    """A bare, cookie-less request straight at the callback — no login flow
    was ever started in this session, so even a well-formed-looking state
    value must still fail."""
    client, m = app_client

    resp = client.get(
        "/auth/callback/discord",
        params={"code": "fake-code", "state": "some-random-guess"},
    )

    assert resp.status_code == 500
    assert m.db.fetchall("SELECT * FROM users") == []


# ── Account-linking callback (/auth/link-callback/{provider}) ───────────────
# CLAUDE.md documents /settings/link/{provider} -> /auth/link-callback/{provider}
# as a deliberately separate path from ordinary login (explicit-only linking),
# so its state handling is checked independently here rather than assumed to
# inherit the login flow's correctness just because the code looks similar.


@pytest.fixture()
def logged_in_client(app_client):
    """A client authenticated as a real local user, ready to drive
    /settings/link/discord — the linking flow requires an existing session
    (_require_user), unlike the ordinary login flow."""
    client, m = app_client
    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
        "VALUES (1, 'local', 'a@example.com', 'a@example.com', %s, true)",
        (m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),),
    )
    login_resp = client.post(
        "/auth/login", data={"email": "a@example.com", "password": "password123"}
    )
    assert login_resp.status_code in (200, 303)
    return client, m


def test_link_callback_rejects_missing_state(logged_in_client):
    client, m = logged_in_client

    resp = client.get("/settings/link/discord", follow_redirects=False)
    assert resp.status_code in (302, 307)

    resp = client.get("/auth/link-callback/discord", params={"code": "fake-code"})

    assert resp.status_code == 500
    # The victim account must not have gained a discord_id from a forged callback.
    row = m.db.fetchone("SELECT discord_id FROM users WHERE id = 1")
    assert row["discord_id"] is None


def test_link_callback_rejects_mismatched_state(logged_in_client):
    client, m = logged_in_client

    resp = client.get("/settings/link/discord", follow_redirects=False)
    assert resp.status_code in (302, 307)

    resp = client.get(
        "/auth/link-callback/discord",
        params={"code": "fake-code", "state": "attacker-supplied-state-value"},
    )

    assert resp.status_code == 500
    row = m.db.fetchone("SELECT discord_id FROM users WHERE id = 1")
    assert row["discord_id"] is None
