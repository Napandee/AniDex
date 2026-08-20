"""
Coverage for issue #207 — MCP server v1: read-only tools + personal access token
(PAT) auth.

Same real-Postgres pattern as tests/test_sessions.py and
tests/test_recommendation_snooze.py: exercises the actual `personal_access_tokens`
table shape from schema.sql, not a hand-duplicated subset of it. Needs a reachable
Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no
Postgres running.

Covers the acceptance criteria from issue #207:
  1. A user can issue and revoke a personal access token from Settings (one-time
     display included — a refresh/back-navigation after creation must not
     re-display the raw token).
  2. Revoking a token actually invalidates it — a subsequent request bearing that
     token is rejected.
  3. A valid PAT can read data through the MCP server's tools, scoped to its
     owning user only.
  4. An invalid/missing/revoked PAT is rejected (401) before reaching any tool.
  5. Cross-user isolation — one user's PAT can never read another user's data,
     for any of the four tools.

The MCP-server-level tests (test_mcp_*) drive the real app through a live uvicorn
server in a background thread, using the official `mcp` Python client SDK exactly
the way a real MCP client (Claude Code, etc.) would — not a hand-rolled JSON-RPC
POST — since the streamable-HTTP transport's session/initialize handshake isn't
something worth re-implementing just for tests. This also exercises the actual
app.on_event("startup")/"shutdown" wiring in main.py that starts/stops
mcp_server's StreamableHTTPSessionManager — a TestClient used without a `with`
block (this repo's usual TestClient pattern, see test_sessions.py's app_client
fixture) never triggers that lifespan machinery, and the MCP session manager
must be running before any request can reach it.
"""

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import pat  # noqa: E402  (needs sys.path insert above)

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
def two_users(pg_conn):
    """A fresh pair of users (ids 1 and 2), with the relevant tables truncated
    first so rows from one test never leak into the next."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "TRUNCATE personal_access_tokens, personal_notes, recommendation_scores, "
            "library_entries, anime, users RESTART IDENTITY CASCADE"
        )
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
            "VALUES (1, 'local', 'alice@example.com', 'alice@example.com', %s, true), "
            "(2, 'local', 'bob@example.com', 'bob@example.com', %s, true)",
            ("x", "x"),
        )
    return (1, 2)


# ── app/pat.py — direct unit coverage ────────────────────────────────────────────


def test_create_token_returns_id_and_raw_token(two_users):
    user_id, _ = two_users
    token_id, raw = pat.create_token(user_id, "My Client")
    assert isinstance(token_id, int)
    assert raw.startswith(pat.TOKEN_PREFIX)


def test_created_token_is_not_stored_in_plaintext(two_users, pg_conn):
    user_id, _ = two_users
    _token_id, raw = pat.create_token(user_id, "My Client")
    with pg_conn.cursor() as cur:
        cur.execute("SELECT token_hash FROM personal_access_tokens WHERE user_id = %s", (user_id,))
        stored_hash = cur.fetchone()[0]
    assert raw not in stored_hash
    assert stored_hash.startswith("$2b$")  # bcrypt


def test_list_tokens_only_returns_that_users_own_active_tokens(two_users):
    user1, user2 = two_users
    pat.create_token(user1, "Alice's client")
    pat.create_token(user2, "Bob's client")

    alice_tokens = pat.list_tokens(user1)
    bob_tokens = pat.list_tokens(user2)

    assert [t["name"] for t in alice_tokens] == ["Alice's client"]
    assert [t["name"] for t in bob_tokens] == ["Bob's client"]


def test_resolve_token_returns_the_owning_user(two_users):
    user_id, _ = two_users
    _token_id, raw = pat.create_token(user_id, "My Client")

    resolved = pat.resolve_token(raw)

    assert resolved is not None
    assert resolved["id"] == user_id
    assert resolved["email"] == "alice@example.com"


def test_resolve_token_returns_none_for_garbage_input(two_users):
    pat.create_token(two_users[0], "My Client")
    assert pat.resolve_token("not-a-real-token") is None
    assert pat.resolve_token(None) is None
    assert pat.resolve_token("") is None


def test_resolve_token_returns_none_for_a_wrong_but_correctly_prefixed_token(two_users):
    """A token that has the right shape (prefix) but wrong secret must still fail
    the bcrypt compare — the prefix check is a cheap short-circuit only, never the
    actual security boundary."""
    user_id, _ = two_users
    pat.create_token(user_id, "My Client")
    assert pat.resolve_token(pat.TOKEN_PREFIX + "wrong-secret-value") is None


def test_revoked_token_no_longer_resolves(two_users):
    """Acceptance criterion: revoking a token actually invalidates it."""
    user_id, _ = two_users
    token_id, raw = pat.create_token(user_id, "My Client")
    assert pat.resolve_token(raw) is not None

    ok = pat.revoke_token(user_id, token_id)

    assert ok is True
    assert pat.resolve_token(raw) is None


def test_revoke_token_is_scoped_to_owning_user(two_users):
    """A user can't revoke another user's token, even by guessing/incrementing an
    id — revoke_token's UPDATE is scoped to (id, user_id)."""
    user1, user2 = two_users
    token_id, raw = pat.create_token(user1, "Alice's client")

    result = pat.revoke_token(user2, token_id)

    assert result is False
    assert pat.resolve_token(raw) is not None  # still fully active


def test_resolve_token_rejects_a_deactivated_user(two_users, pg_conn):
    """Issue #85's is_active flag — a deactivated account's PAT stops working
    immediately, same as its sessions do."""
    user_id, _ = two_users
    _token_id, raw = pat.create_token(user_id, "My Client")
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE users SET is_active = false WHERE id = %s", (user_id,))

    assert pat.resolve_token(raw) is None


def test_resolve_token_bumps_last_used_at(two_users, pg_conn):
    user_id, _ = two_users
    token_id, raw = pat.create_token(user_id, "My Client")
    with pg_conn.cursor() as cur:
        cur.execute("SELECT last_used_at FROM personal_access_tokens WHERE id = %s", (token_id,))
        before = cur.fetchone()[0]
    assert before is None

    pat.resolve_token(raw)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT last_used_at FROM personal_access_tokens WHERE id = %s", (token_id,))
        after = cur.fetchone()[0]
    assert after is not None


# ── Full-app coverage via TestClient — the real /settings/tokens routes ─────────


@pytest.fixture()
def app_client(pg_conn, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    import app.main as m
    from fastapi.testclient import TestClient

    with pg_conn.cursor() as cur:
        cur.execute(
            "TRUNCATE personal_access_tokens, sessions, personal_notes, "
            "recommendation_scores, library_entries, anime, users RESTART IDENTITY CASCADE"
        )

    return TestClient(m.app), m


def _login(client, m, email, password, user_id):
    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
        "VALUES (%s, 'local', %s, %s, %s, true) ON CONFLICT (id) DO NOTHING",
        (user_id, email, email, m.bcrypt.hashpw(password.encode(), m.bcrypt.gensalt()).decode()),
    )
    resp = client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=False)
    assert resp.status_code == 303, resp.text


def test_create_token_route_shows_raw_token_once_then_hides_it(app_client):
    client, m = app_client
    _login(client, m, "alice@example.com", "password123", 1)

    resp = client.post("/settings/tokens", data={"name": "Claude Code"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings/tokens/created"

    first_view = client.get("/settings/tokens/created")
    assert first_view.status_code == 200
    assert pat.TOKEN_PREFIX in first_view.text
    assert "Claude Code" in first_view.text

    # Second view (simulating a refresh/back-navigation) must not re-show the token
    # — pop-once semantics, same as issue #83's TOTP recovery codes.
    second_view = client.get("/settings/tokens/created", follow_redirects=False)
    assert second_view.status_code == 303
    assert second_view.headers["location"] == "/settings"


def test_created_token_actually_works_and_matches_db_row(app_client):
    client, m = app_client
    _login(client, m, "alice@example.com", "password123", 1)
    client.post("/settings/tokens", data={"name": "Claude Code"}, follow_redirects=False)
    page = client.get("/settings/tokens/created").text

    import re
    match = re.search(rf"({pat.TOKEN_PREFIX}[A-Za-z0-9_-]+)", page)
    assert match, "raw token not found in one-time-display page"
    raw_token = match.group(1)

    resolved = pat.resolve_token(raw_token)
    assert resolved is not None
    assert resolved["id"] == 1


def test_revoke_token_route_is_scoped_to_owning_user(app_client):
    client, m = app_client
    _login(client, m, "alice@example.com", "password123", 1)
    token_id, raw = pat.create_token(1, "Alice's token")

    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
        "VALUES (2, 'local', 'bob@example.com', 'bob@example.com', %s, true)",
        (m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),),
    )

    # Alice (logged in) tries to revoke her own token id — should succeed.
    resp = client.post(f"/settings/tokens/{token_id}/revoke", follow_redirects=False)
    assert resp.status_code == 303
    assert pat.resolve_token(raw) is None


def test_revoke_token_route_no_ops_for_another_users_token(app_client):
    client, m = app_client
    _login(client, m, "bob@example.com", "password123", 2)
    # A token that belongs to user 1 (not logged in here).
    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
        "VALUES (1, 'local', 'alice@example.com', 'alice@example.com', %s, true)",
        (m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),),
    )
    token_id, raw = pat.create_token(1, "Alice's token")

    resp = client.post(f"/settings/tokens/{token_id}/revoke", follow_redirects=False)
    assert resp.status_code == 303  # always redirects, no ownership error leaked
    assert pat.resolve_token(raw) is not None  # untouched — still resolves


# ── MCP server — live end-to-end coverage via a real MCP client ─────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_app(pg_conn):
    """Boots the real app (including its startup/shutdown events, which start and
    stop mcp_server's StreamableHTTPSessionManager) via a live uvicorn server in a
    background thread. Module-scoped — one server for every test in this section,
    each test uses its own users/tokens so they don't interfere."""
    pytest.importorskip("uvicorn")
    import uvicorn

    os.environ["DATABASE_URL"] = DATABASE_URL
    os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
    os.environ.setdefault("MOCK_ANILIST", "1")

    with pg_conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(SCHEMA_SQL)

    import app.main as m

    port = _free_port()
    config = uvicorn.Config(m.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import httpx
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"{base_url}/auth/login", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)
    else:
        pytest.fail("live app never became reachable")

    yield base_url, m

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture()
def mcp_two_users(live_app):
    """Fresh users + seeded, clearly-distinguishable per-user data for the MCP
    tool tests below."""
    _base_url, m = live_app
    m.db.execute(
        "TRUNCATE personal_access_tokens, personal_notes, recommendation_scores, "
        "library_entries, anime, users RESTART IDENTITY CASCADE"
    )
    m.db.execute(
        "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
        "VALUES (1, 'local', 'alice@example.com', 'alice@example.com', 'x', true), "
        "(2, 'local', 'bob@example.com', 'bob@example.com', 'x', true)"
    )
    m.db.execute(
        "INSERT INTO anime (id, title_romaji, format, episodes, duration, genres, average_score) VALUES "
        "(101, 'Alice Only Anime', 'TV', 12, 24, '[\"Action\"]', 80), "
        "(102, 'Bob Only Anime', 'TV', 13, 24, '[\"Romance\"]', 70)"
    )
    m.db.execute(
        "INSERT INTO library_entries (user_id, anime_id, status, score, progress) VALUES "
        "(1, 101, 'COMPLETED', 4.5, 12), (2, 102, 'COMPLETED', 3.0, 13)"
    )
    m.db.execute(
        "INSERT INTO personal_notes (user_id, anime_id, notes) VALUES (1, 101, 'Alice private note')"
    )
    _tid1, alice_token = pat.create_token(1, "alice-test-client")
    _tid2, bob_token = pat.create_token(2, "bob-test-client")
    return alice_token, bob_token


async def _mcp_call(base_url, token, tool_name, args=None):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(f"{base_url}/mcp", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, args or {})
            # Prefer structuredContent — reliable for list-typed tool returns
            # regardless of how many text content blocks FastMCP emits (it emits
            # one block per list item, so content[0].text is only the FIRST row
            # for a >1-item list, not the whole list). structuredContent always
            # carries the full, properly-typed {"result": ...} payload.
            if result.structuredContent is not None:
                return result.structuredContent.get("result")
            if result.content:
                return json.loads(result.content[0].text)
            return None


async def _mcp_list_tools(base_url, token):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(f"{base_url}/mcp", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return sorted(t.name for t in tools.tools)


async def _mcp_expect_auth_failure(base_url, token):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with pytest.raises(Exception):
        async with streamablehttp_client(f"{base_url}/mcp", headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()


def test_mcp_exposes_only_the_four_read_only_tools(live_app, mcp_two_users):
    import asyncio
    base_url, _m = live_app
    alice_token, _bob_token = mcp_two_users

    tools = asyncio.run(_mcp_list_tools(base_url, alice_token))

    assert tools == sorted(
        ["list_library_entries", "list_personal_notes", "list_recommendations", "get_stats"]
    )
    # No write-capable tool name should ever sneak in here — issue #171/#207's
    # explicit "read-only for v1" decision.
    for name in tools:
        assert not any(verb in name for verb in ("create", "update", "delete", "set", "dismiss", "add"))


def test_mcp_valid_token_reads_only_that_users_own_library(live_app, mcp_two_users):
    import asyncio
    base_url, _m = live_app
    alice_token, bob_token = mcp_two_users

    alice_lib = asyncio.run(_mcp_call(base_url, alice_token, "list_library_entries"))
    bob_lib = asyncio.run(_mcp_call(base_url, bob_token, "list_library_entries"))

    assert [row["anime_id"] for row in alice_lib] == [101]
    assert [row["anime_id"] for row in bob_lib] == [102]


def test_mcp_valid_token_reads_only_that_users_own_personal_notes(live_app, mcp_two_users):
    """Cross-user isolation for personal notes specifically — Bob has none, and
    must never see Alice's."""
    import asyncio
    base_url, _m = live_app
    alice_token, bob_token = mcp_two_users

    alice_notes = asyncio.run(_mcp_call(base_url, alice_token, "list_personal_notes"))
    bob_notes = asyncio.run(_mcp_call(base_url, bob_token, "list_personal_notes"))

    assert len(alice_notes) == 1
    assert alice_notes[0]["notes"] == "Alice private note"
    assert bob_notes == []


def test_mcp_stats_are_scoped_per_user(live_app, mcp_two_users):
    import asyncio
    base_url, _m = live_app
    alice_token, bob_token = mcp_two_users

    alice_stats = asyncio.run(_mcp_call(base_url, alice_token, "get_stats"))
    bob_stats = asyncio.run(_mcp_call(base_url, bob_token, "get_stats"))

    assert alice_stats["completed"] == 1
    assert alice_stats["mean_score"] == 4.5
    assert bob_stats["completed"] == 1
    assert bob_stats["mean_score"] == 3.0


def test_mcp_missing_token_is_rejected(live_app, mcp_two_users):
    import asyncio
    base_url, _m = live_app
    asyncio.run(_mcp_expect_auth_failure(base_url, None))


def test_mcp_invalid_token_is_rejected(live_app, mcp_two_users):
    import asyncio
    base_url, _m = live_app
    asyncio.run(_mcp_expect_auth_failure(base_url, pat.TOKEN_PREFIX + "totally-bogus"))


def test_mcp_revoked_token_is_rejected(live_app, mcp_two_users):
    import asyncio
    base_url, m = live_app
    alice_token, _bob_token = mcp_two_users
    row = m.db.fetchone(
        "SELECT id FROM personal_access_tokens WHERE token_hash IS NOT NULL AND user_id = 1 "
        "ORDER BY created_at DESC LIMIT 1"
    )
    pat.revoke_token(1, row["id"])

    asyncio.run(_mcp_expect_auth_failure(base_url, alice_token))
