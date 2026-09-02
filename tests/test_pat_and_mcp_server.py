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

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

from app import pat  # noqa: E402  (needs sys.path insert above)


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


# ── PAT scope (issue #208) ───────────────────────────────────────────────────


def test_create_token_defaults_to_read_scope(two_users):
    """The safe default per #208's PAT-scoping decision — a token created
    without explicitly requesting read_write must never come out write-capable."""
    user_id, _ = two_users
    _token_id, raw = pat.create_token(user_id, "My Client")
    assert pat.resolve_token(raw)["pat_scope"] == pat.SCOPE_READ


def test_create_token_honors_explicit_read_write_scope(two_users):
    user_id, _ = two_users
    _token_id, raw = pat.create_token(user_id, "My Client", scope=pat.SCOPE_READ_WRITE)
    assert pat.resolve_token(raw)["pat_scope"] == pat.SCOPE_READ_WRITE


def test_create_token_falls_back_to_read_for_an_invalid_scope_value(two_users):
    """A malformed/unexpected scope value (e.g. a tampered form submission)
    must never silently mint a write-capable token — it falls back to the safe
    default instead of erroring or trusting the input."""
    user_id, _ = two_users
    _token_id, raw = pat.create_token(user_id, "My Client", scope="admin")
    assert pat.resolve_token(raw)["pat_scope"] == pat.SCOPE_READ


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


def test_create_token_route_honors_read_write_scope_selection(app_client):
    client, m = app_client
    _login(client, m, "alice@example.com", "password123", 1)

    client.post(
        "/settings/tokens", data={"name": "Claude Code", "scope": "read_write"}, follow_redirects=False
    )
    page = client.get("/settings/tokens/created").text

    import re
    match = re.search(rf"({pat.TOKEN_PREFIX}[A-Za-z0-9_-]+)", page)
    raw_token = match.group(1)
    assert pat.resolve_token(raw_token)["pat_scope"] == pat.SCOPE_READ_WRITE


def test_create_token_route_defaults_to_read_scope_when_omitted(app_client):
    client, m = app_client
    _login(client, m, "alice@example.com", "password123", 1)

    client.post("/settings/tokens", data={"name": "Claude Code"}, follow_redirects=False)
    page = client.get("/settings/tokens/created").text

    import re
    match = re.search(rf"({pat.TOKEN_PREFIX}[A-Za-z0-9_-]+)", page)
    raw_token = match.group(1)
    assert pat.resolve_token(raw_token)["pat_scope"] == pat.SCOPE_READ


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
    os.environ.setdefault("ANILIST_MOCK", "1")  # see tests/conftest.py's own note

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
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    # Issue #326 (SDK v1 -> v2): streamablehttp_client(url, headers=...) is gone —
    # renamed streamable_http_client, and headers are now set via a pre-configured
    # httpx2.AsyncClient passed as http_client= rather than a headers= kwarg on the
    # transport itself. Also yields a 2-tuple (read, write) now, not v1's 3-tuple
    # (read, write, get_session_id) — confirmed directly against the installed v2
    # package (mcp.client.streamable_http.TransportStreams) before this change.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx2.AsyncClient(headers=headers, follow_redirects=True) as http_client:
        async with streamable_http_client(f"{base_url}/mcp", http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, args or {})
                # Prefer structured_content — reliable for list-typed tool returns
                # regardless of how many text content blocks the SDK emits (it emits
                # one block per list item, so content[0].text is only the FIRST row
                # for a >1-item list, not the whole list). structured_content always
                # carries the full, properly-typed {"result": ...} payload. (Issue
                # #326, SDK v1 -> v2: this field was camelCase structuredContent on
                # v1 — confirmed the snake_case rename directly against the
                # installed v2 package before relying on it here.)
                if result.structured_content is not None:
                    return result.structured_content.get("result")
                if result.content:
                    return json.loads(result.content[0].text)
                return None


async def _mcp_list_tools(base_url, token):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(headers=headers, follow_redirects=True) as http_client:
        async with streamable_http_client(f"{base_url}/mcp", http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return sorted(t.name for t in tools.tools)


async def _mcp_expect_auth_failure(base_url, token):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with pytest.raises(Exception):
        async with httpx2.AsyncClient(headers=headers, follow_redirects=True) as http_client:
            async with streamable_http_client(f"{base_url}/mcp", http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()


def test_mcp_exposes_the_four_read_tools_and_five_write_tools(live_app, mcp_two_users):
    """Issue #208 deliberately added five write-capable tools on top of #207's
    four read-only ones (superseding the older read-only-for-v1 assertion this
    test used to make) — assert the exact tool surface instead, so an
    accidental extra tool (e.g. a filter-based bulk write someone adds later)
    would fail this test immediately."""
    import asyncio
    base_url, _m = live_app
    alice_token, _bob_token = mcp_two_users

    tools = asyncio.run(_mcp_list_tools(base_url, alice_token))

    assert tools == sorted([
        "list_library_entries", "list_personal_notes", "list_recommendations", "get_stats",
        "update_personal_notes", "bulk_apply_tags", "set_rating", "set_status", "set_progress",
    ])


# ── Write tools (issue #208) ─────────────────────────────────────────────────


@pytest.fixture()
def mcp_two_users_write(live_app):
    """Same seed shape as mcp_two_users, but both tokens are issued with
    read_write scope so the write-tool tests below can actually reach them —
    mcp_two_users's tokens are deliberately read-only (pat.create_token's
    default), matching what a real #207-era-issued token still is today."""
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
        "(1, 101, 'WATCHING', NULL, 3), (2, 102, 'WATCHING', NULL, 4)"
    )
    _tid1, alice_token = pat.create_token(1, "alice-write-client", scope=pat.SCOPE_READ_WRITE)
    _tid2, bob_token = pat.create_token(2, "bob-write-client", scope=pat.SCOPE_READ_WRITE)
    return alice_token, bob_token


async def _mcp_call_expect_error(base_url, token, tool_name, args=None):
    """Like _mcp_call, but for calls expected to fail as a tool-level error
    (ToolError) rather than succeed — returns the raw CallToolResult so the
    caller can assert on is_error / the error text, since the SDK surfaces tool
    errors as a normal (is_error=True) result rather than raising on the client
    side (verified unchanged on SDK v2, issue #326)."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx2.AsyncClient(headers=headers, follow_redirects=True) as http_client:
        async with streamable_http_client(f"{base_url}/mcp", http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(tool_name, args or {})


def test_mcp_update_personal_notes_persists_and_is_scoped_to_owner(live_app, mcp_two_users_write):
    """Acceptance criterion: a write tool call with explicit IDs succeeds and
    actually persists the change."""
    import asyncio
    base_url, m = live_app
    alice_token, _bob_token = mcp_two_users_write

    result = asyncio.run(_mcp_call(base_url, alice_token, "update_personal_notes", {
        "anime_id": 101, "notes": "Written via MCP", "personal_tags": "cozy, favorite",
    }))
    assert result["ok"] is True

    row = m.db.fetchone(
        "SELECT notes, personal_tags FROM personal_notes WHERE user_id = 1 AND anime_id = 101"
    )
    assert row["notes"] == "Written via MCP"
    assert row["personal_tags"] == ["cozy", "favorite"]  # jsonb column, decoded by psycopg2 already


def test_mcp_update_personal_notes_sets_and_preserves_anilist_id_override(live_app, mcp_two_users_write):
    """anilist_id_override (issue #159 — the CR-sync season-mismatch fix) must
    round-trip through the MCP tool like every other field: a call that passes
    it sets it, and a later call that passes it again keeps it, rather than the
    tool silently having no way to carry this field at all."""
    import asyncio
    base_url, m = live_app
    alice_token, _bob_token = mcp_two_users_write

    asyncio.run(_mcp_call(base_url, alice_token, "update_personal_notes", {
        "anime_id": 101, "notes": "season 2 fix", "anilist_id_override": 999,
    }))
    row = m.db.fetchone(
        "SELECT notes, anilist_id_override FROM personal_notes WHERE user_id = 1 AND anime_id = 101"
    )
    assert row["anilist_id_override"] == 999
    assert row["notes"] == "season 2 fix"

    # A second call that passes the same override again alongside a different
    # field must keep it — this is a full-replace endpoint, so the caller has
    # to keep passing it, but the tool itself must not drop it on the floor
    # when it IS passed.
    asyncio.run(_mcp_call(base_url, alice_token, "update_personal_notes", {
        "anime_id": 101, "notes": "still season 2", "anilist_id_override": 999,
    }))
    row = m.db.fetchone(
        "SELECT notes, anilist_id_override FROM personal_notes WHERE user_id = 1 AND anime_id = 101"
    )
    assert row["anilist_id_override"] == 999
    assert row["notes"] == "still season 2"


def test_mcp_update_personal_notes_clears_anilist_id_override_when_omitted(live_app, mcp_two_users_write):
    """Full-replace semantics, consistent with every other field on this tool
    and with the HTTP routes it mirrors: omitting anilist_id_override on a
    later call clears a previously-set value, it doesn't leave it alone."""
    import asyncio
    base_url, m = live_app
    alice_token, _bob_token = mcp_two_users_write

    asyncio.run(_mcp_call(base_url, alice_token, "update_personal_notes", {
        "anime_id": 101, "anilist_id_override": 999,
    }))
    row = m.db.fetchone("SELECT anilist_id_override FROM personal_notes WHERE user_id = 1 AND anime_id = 101")
    assert row["anilist_id_override"] == 999

    asyncio.run(_mcp_call(base_url, alice_token, "update_personal_notes", {
        "anime_id": 101, "notes": "changed my mind about the override",
    }))
    row = m.db.fetchone(
        "SELECT notes, anilist_id_override FROM personal_notes WHERE user_id = 1 AND anime_id = 101"
    )
    assert row["anilist_id_override"] is None
    assert row["notes"] == "changed my mind about the override"


def test_mcp_update_personal_notes_cannot_write_another_users_row(live_app, mcp_two_users_write):
    """Cross-user isolation: Bob's token can only ever write anime_id=101 under
    Bob's own user_id — even though 101 is an anime Alice owns a library entry
    for, the write lands in Bob's personal_notes row, never Alice's, because
    every write helper scopes on the token-resolved user_id, not a caller-
    supplied one (there is no user_id parameter on any tool)."""
    import asyncio
    base_url, m = live_app
    _alice_token, bob_token = mcp_two_users_write

    asyncio.run(_mcp_call(base_url, bob_token, "update_personal_notes", {
        "anime_id": 101, "notes": "Bob trying to write Alice's anime",
    }))

    alice_row = m.db.fetchone("SELECT notes FROM personal_notes WHERE user_id = 1 AND anime_id = 101")
    bob_row = m.db.fetchone("SELECT notes FROM personal_notes WHERE user_id = 2 AND anime_id = 101")
    assert alice_row is None  # Alice's row was never touched
    assert bob_row["notes"] == "Bob trying to write Alice's anime"  # landed under Bob's own user_id


def test_mcp_bulk_apply_tags_requires_explicit_ids_and_persists(live_app, mcp_two_users_write):
    import asyncio
    base_url, m = live_app
    alice_token, _bob_token = mcp_two_users_write

    result = asyncio.run(_mcp_call(base_url, alice_token, "bulk_apply_tags", {
        "anime_ids": [101], "tags": ["binged"],
    }))
    assert result == {"ok": True, "count": 1}

    row = m.db.fetchone("SELECT personal_tags FROM personal_notes WHERE user_id = 1 AND anime_id = 101")
    assert row["personal_tags"] == ["binged"]  # jsonb column, decoded by psycopg2 already


def test_mcp_bulk_apply_tags_rejects_empty_id_list(live_app, mcp_two_users_write):
    """An empty anime_ids list must not be treated as an implicit "no scope
    restriction" / apply-to-nothing-but-silently-ok call — it's rejected
    outright, same posture as the /api/anime/bulk-tags route it mirrors."""
    import asyncio
    base_url, _m = live_app
    alice_token, _bob_token = mcp_two_users_write

    result = asyncio.run(_mcp_call_expect_error(base_url, alice_token, "bulk_apply_tags", {
        "anime_ids": [], "tags": ["binged"],
    }))
    assert result.is_error is True


def test_mcp_bulk_apply_tags_rejects_more_ids_than_the_per_call_cap(live_app, mcp_two_users_write):
    """An explicit id list is still bounded per the guardrail's letter even at
    hundreds of ids, but a single call fanning out that many one-row-at-a-time
    writes is capped (_MAX_BULK_TAG_IDS in app/mcp_server.py) — split into
    multiple calls instead."""
    import asyncio
    from app.mcp_server import _MAX_BULK_TAG_IDS
    base_url, _m = live_app
    alice_token, _bob_token = mcp_two_users_write

    too_many_ids = list(range(1, _MAX_BULK_TAG_IDS + 2))
    result = asyncio.run(_mcp_call_expect_error(base_url, alice_token, "bulk_apply_tags", {
        "anime_ids": too_many_ids, "tags": ["binged"],
    }))
    assert result.is_error is True


def test_mcp_set_rating_persists_and_is_user_scoped(live_app, mcp_two_users_write):
    import asyncio
    base_url, m = live_app
    alice_token, bob_token = mcp_two_users_write

    result = asyncio.run(_mcp_call(base_url, alice_token, "set_rating", {"anime_id": 101, "score": 4}))
    assert result == {"ok": True, "anime_id": 101, "score": 4}

    alice_score = m.db.fetchone("SELECT score FROM library_entries WHERE user_id = 1 AND anime_id = 101")["score"]
    bob_score = m.db.fetchone("SELECT score FROM library_entries WHERE user_id = 2 AND anime_id = 102")["score"]
    assert float(alice_score) == 4.0
    assert bob_score is None  # untouched by Alice's call


def test_mcp_set_status_persists(live_app, mcp_two_users_write):
    import asyncio
    base_url, m = live_app
    alice_token, _bob_token = mcp_two_users_write

    result = asyncio.run(_mcp_call(base_url, alice_token, "set_status", {"anime_id": 101, "status": "completed"}))
    assert result == {"ok": True, "anime_id": 101, "status": "COMPLETED"}

    status = m.db.fetchone("SELECT status FROM library_entries WHERE user_id = 1 AND anime_id = 101")["status"]
    assert status == "COMPLETED"


def test_mcp_set_status_rejects_invalid_status(live_app, mcp_two_users_write):
    import asyncio
    base_url, _m = live_app
    alice_token, _bob_token = mcp_two_users_write

    result = asyncio.run(_mcp_call_expect_error(base_url, alice_token, "set_status", {
        "anime_id": 101, "status": "NOT_A_REAL_STATUS",
    }))
    assert result.is_error is True


def test_mcp_set_progress_persists(live_app, mcp_two_users_write):
    import asyncio
    base_url, m = live_app
    alice_token, _bob_token = mcp_two_users_write

    result = asyncio.run(_mcp_call(base_url, alice_token, "set_progress", {"anime_id": 101, "progress": 7}))
    assert result == {"ok": True, "anime_id": 101, "progress": 7}

    progress = m.db.fetchone("SELECT progress FROM library_entries WHERE user_id = 1 AND anime_id = 101")["progress"]
    assert progress == 7


# Every write tool by name, with one representative valid-shaped argument set —
# used below to assert read-scope rejection across ALL of them, not just one.
# If a new write tool is ever added without wiring in _require_write_scope(),
# test_mcp_exposes_the_four_read_tools_and_five_write_tools will fail on the
# tool-name-set mismatch first, which is the prompt to add it here too.
_WRITE_TOOL_CALLS = {
    "update_personal_notes": {"anime_id": 101, "notes": "x"},
    "bulk_apply_tags": {"anime_ids": [101], "tags": ["x"]},
    "set_rating": {"anime_id": 101, "score": 3},
    "set_status": {"anime_id": 101, "status": "WATCHING"},
    "set_progress": {"anime_id": 101, "progress": 5},
}


def test_mcp_write_tool_rejects_a_read_only_token(live_app, mcp_two_users):
    """mcp_two_users issues plain read-scoped tokens (pat.create_token's
    default) — a write tool call with one of those must be rejected, not
    silently downgraded to a no-op or allowed through. Covers every write tool
    by name (see _WRITE_TOOL_CALLS), not just one — a future write tool that
    forgets to call _require_write_scope() should fail here."""
    import asyncio
    base_url, m = live_app
    alice_token, _bob_token = mcp_two_users

    for tool_name, args in _WRITE_TOOL_CALLS.items():
        result = asyncio.run(_mcp_call_expect_error(base_url, alice_token, tool_name, args))
        assert result.is_error is True, f"{tool_name} did not reject a read-only token"

    # And nothing was actually written by any of the calls above.
    progress = m.db.fetchone("SELECT progress FROM library_entries WHERE user_id = 1 AND anime_id = 101")["progress"]
    assert progress == 12  # unchanged from mcp_two_users' seed value
    score = m.db.fetchone("SELECT score FROM library_entries WHERE user_id = 1 AND anime_id = 101")["score"]
    assert float(score) == 4.5  # unchanged from mcp_two_users' seed value
    notes_row = m.db.fetchone("SELECT id FROM personal_notes WHERE user_id = 1 AND anime_id = 101 AND notes = 'x'")
    assert notes_row is None


def test_mcp_read_tools_still_work_with_a_read_only_token(live_app, mcp_two_users):
    """Scope is additive, not a replacement gate — a read-only token must still
    be able to call every #207 read tool exactly as before."""
    import asyncio
    base_url, _m = live_app
    alice_token, _bob_token = mcp_two_users

    result = asyncio.run(_mcp_call(base_url, alice_token, "list_library_entries"))
    assert [row["anime_id"] for row in result] == [101]


# ── Structural guarantee: every write tool's schema requires explicit ids ───
# Acceptance criterion from issue #208 / CLAUDE.md's MCP guardrail: "write tools
# must require explicit ID lists — never wildcard or filter-based bulk writes."
# This asserts it at the JSON-schema level FastMCP actually generates and hands
# to a real MCP client — not just "the tests above happened to pass explicit
# ids", which would say nothing about whether some OTHER call shape is also
# accepted.


def test_write_tool_schemas_require_explicit_ids_not_filters():
    from app.mcp_server import _build_mcp

    instance = _build_mcp()
    tools = {t.name: t for t in instance._tool_manager.list_tools()}

    single_id_tools = {"update_personal_notes", "set_rating", "set_status", "set_progress"}
    assert single_id_tools <= set(tools)
    assert "bulk_apply_tags" in tools

    for name in single_id_tools:
        schema = tools[name].parameters
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        assert "anime_id" in props, f"{name} must take an explicit anime_id"
        assert props["anime_id"]["type"] == "integer"
        assert "anime_id" in required, f"{name}'s anime_id must be required, not optional"

    bulk_schema = tools["bulk_apply_tags"].parameters
    bulk_props = bulk_schema.get("properties", {})
    bulk_required = set(bulk_schema.get("required", []))
    assert "anime_ids" in bulk_props
    assert bulk_props["anime_ids"]["type"] == "array"
    assert bulk_props["anime_ids"]["items"]["type"] == "integer"
    assert "anime_ids" in bulk_required, "bulk_apply_tags's anime_ids must be required, not optional"

    # No write tool anywhere exposes a filter/query/wildcard-shaped parameter —
    # the actual disallowed shape the guardrail exists to rule out.
    forbidden_param_names = ("filter", "query", "search", "where", "match", "all", "wildcard")
    for name in single_id_tools | {"bulk_apply_tags"}:
        schema = tools[name].parameters
        for prop_name in schema.get("properties", {}):
            lowered = prop_name.lower()
            assert not any(f in lowered for f in forbidden_param_names), (
                f"{name} exposes {prop_name!r} — looks like a filter/query parameter, "
                "which the explicit-ID-only guardrail disallows for write tools"
            )


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
