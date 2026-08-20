"""MCP server v1 (issue #207) — read-only tools exposing a user's own library,
personal notes, stats, and recommendation data to external AI clients (Claude Code,
self-configured MCP clients), authenticated via the per-user personal access tokens
issued in Settings (see app/pat.py).

Architecture choice — mode of the existing app, not a separate process/container:
mounted at /mcp on the same FastAPI app (see main.py's `app.mount("/mcp", ...)`) and
running in the same uvicorn process. This repo already treats "one more container" as
something that needs a real justification, not a default (see CLAUDE.md's "One sync
path, not two" decision, made after removing a genuinely redundant second container).
A read-only MCP server that has no background work of its own and reads the exact
same tables/DATABASE_URL the rest of the app already reads doesn't clear that bar —
it's naturally just another route surface, like /api/*.

Uses the official MCP Python SDK (`mcp`, added to requirements.txt) rather than
hand-rolling the streamable-HTTP JSON-RPC protocol — FastMCP's `add_tool()` API
(the same registration `@tool()` sugar calls internally; used directly here so a
fresh instance can be built per _build_mcp() call, see below) handles tool schema
generation, request routing, and the streamable-HTTP transport; only the auth layer
below is this app's own code.

Auth — deliberately NOT using the SDK's own `auth=`/`token_verifier=` machinery
(a `TokenVerifier` protocol + `AuthSettings`, built for OAuth-flavored
resource-server setups with issuer URLs, protected-resource metadata, optional
auth-server routes, etc.). That machinery *could* be bent into a PAT-only shape, but
per CLAUDE.md's MCP guardrail (issue #171) this app is explicitly not taking on any
OAuth authorization-server role — a small, plain Bearer-token ASGI wrapper
(_BearerAuthGate below) keeps that true by construction, not by carefully avoiding
parts of a heavier facility that mostly exists for OAuth. It validates the
Authorization header against app/pat.py's resolve_token (bcrypt-hashed PATs, same
standard as this app's passwords) and makes the resolved user available to every tool
via a contextvar — there is no code path in this file that can resolve a token to
any user other than the one who issued it, so cross-user reads are structurally not
possible here, not just policy.

Tool surface is read-only by design (issue #171's decision, restated in CLAUDE.md's
guardrail): no tool in this file writes anything, and none should ever be added here
without a fresh scoped decision — see the held write-tools follow-up issue mentioned
in #207.
"""

import asyncio
import datetime
import decimal
import json as json_mod
import logging
from contextvars import ContextVar

from mcp.server.fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool

from app import db, pat

log = logging.getLogger(__name__)

_INSTRUCTIONS = (
    "Read-only access to your AniDex library, personal notes, stats, and "
    "recommendation scores. Every tool is scoped to the account that owns the "
    "personal access token used to authenticate for this connection — there is "
    "no tool here that can read another user's data, and there are no "
    "write/mutating tools in this server."
)


def _build_mcp() -> FastMCP:
    """A fresh FastMCP instance with all four read tools registered.

    Deliberately a factory, not a module-level singleton: `mcp.server.streamable_
    http_manager.StreamableHTTPSessionManager.run()` (the background loop that
    services every request) is a one-shot async context manager — a given instance
    raises if entered a second time. This app's own test suite starts/stops the
    whole FastAPI app's lifespan once per test function (see e.g.
    tests/test_totp_2fa.py's `client` fixture, `with TestClient(app) as c:`), which
    calls start()/stop() below every time — so the MCP side needs a genuinely new
    session manager each time too, not a single instance reused across many
    start/stop cycles. In production this factory still only ever runs once, at
    the one real app startup, so there's no behavior change there — this only
    matters for repeated start/stop cycles within a single process, which normal
    operation never does."""
    instance = FastMCP(
        name="AniDex",
        instructions=_INSTRUCTIONS,
        stateless_http=True,   # each request is independent — matches a read-only
                                # tool server with no need to remember state
                                # between calls
        json_response=True,    # plain JSON responses rather than an SSE stream,
                                # simpler for both this app's tests and most MCP
                                # HTTP clients
        streamable_http_path="/",  # mounted at /mcp already (see main.py's
                                    # app.mount) — without this the effective URL
                                    # would be /mcp/mcp, since FastMCP's own
                                    # default route path is also /mcp
    )
    for fn in _TOOL_FUNCTIONS:
        instance.add_tool(fn)
    return instance


# ── Auth: PAT -> resolved user, made available to every tool call ───────────────

_current_user_var: ContextVar[dict | None] = ContextVar("mcp_current_user", default=None)


def _require_user() -> dict:
    """The user resolved from this request's PAT. Every tool below calls this
    first. Raising here (rather than returning None and having every tool check)
    should be unreachable in practice — _BearerAuthGate rejects any request without
    a valid, active token's user resolved *before* it ever reaches a tool handler —
    but a tool implementation bug that somehow skipped the gate should fail loudly
    with "no user", not silently query with a null/missing user_id."""
    user = _current_user_var.get()
    if user is None:
        raise RuntimeError("MCP tool invoked without an authenticated user — this is a bug in _BearerAuthGate, not a client error")
    return user


_WWW_AUTHENTICATE = (
    'Bearer error="invalid_token", '
    'error_description="A valid Authorization: Bearer <token> header is required"'
)


async def _send_401(send) -> None:
    body = json_mod.dumps(
        {"error": "invalid_token", "error_description": "Missing, malformed, or revoked personal access token"}
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", _WWW_AUTHENTICATE.encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _BearerAuthGate:
    """Pure-ASGI wrapper around the FastMCP streamable-HTTP app. Extracts
    `Authorization: Bearer <token>`, resolves it via app.pat.resolve_token (bcrypt
    compare against every currently-active token — see that module's docstring),
    and either rejects with 401 or sets the resolved user as a contextvar for the
    duration of the request before calling through to the real MCP app.

    The contextvar is set here, in the outermost ASGI call, before FastMCP's own
    session-manager/task-group machinery does anything with the request — Python's
    asyncio.Task captures a copy of the *current* context at task-creation time, so
    any task the session manager spawns while handling this request (including the
    one that eventually invokes a tool function) still sees the value set here, even
    though it's a different Task object than this one. Verified live end-to-end
    (multiple concurrent PATs, one MCP client session each) as part of #207 — not
    just theorized from asyncio's contextvars semantics.

    Holds a swappable reference to the inner app (`self._app`, set via set_app())
    rather than a fixed one passed to __init__ — see start()/_build_mcp()'s
    docstring for why: this module builds a brand-new FastMCP/session-manager
    instance on every start(), and this is the one long-lived object (constructed
    once, mounted once in main.py) whose target needs to follow along.
    """

    def __init__(self):
        self._app = None

    def set_app(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # Covers the ASGI "lifespan" scope type — this app manages the MCP
            # session manager's own run() lifecycle explicitly from main.py's
            # startup/shutdown events (see start()/stop() below), not via this
            # mounted sub-app's lifespan, so there's nothing to gate here either
            # way. Just pass through, if there's currently a live inner app at all.
            if self._app is not None:
                await self._app(scope, receive, send)
            return

        if self._app is None:
            # Only reachable if a request somehow lands before start() has run
            # (or after stop() has torn it down) — not a normal operating state.
            await _send_401(send)
            return

        header_map = dict(scope.get("headers") or [])
        raw_auth = header_map.get(b"authorization", b"").decode("latin-1")
        token = raw_auth[7:] if raw_auth.lower().startswith("bearer ") else None

        user = await run_in_threadpool(pat.resolve_token, token) if token else None
        if user is None:
            await _send_401(send)
            return

        reset_token = _current_user_var.set(user)
        try:
            await self._app(scope, receive, send)
        finally:
            _current_user_var.reset(reset_token)


# ── JSON-safety for tool return values ───────────────────────────────────────────
# psycopg2's RealDictCursor hands back datetime.date/datetime and Decimal objects
# that aren't directly JSON-serializable — converted explicitly here rather than
# relying on whatever FastMCP's own result-serialization path happens to do with
# them, so a tool's output shape doesn't quietly depend on an SDK implementation
# detail.


def _serialize_value(value):
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


def _serialize_row(row: dict) -> dict:
    return {k: _serialize_value(v) for k, v in row.items()}


# ── Tools ─────────────────────────────────────────────────────────────────────
# All four read straight off the same tables the rest of the app reads (schema.sql),
# each scoped to _require_user()["id"] in the WHERE clause — no tool here accepts a
# user_id parameter from the caller, which is what makes cross-user reads structurally
# impossible rather than just policy. DB calls are synchronous psycopg2 (app/db.py,
# shared with the rest of the app) so each is dispatched via run_in_threadpool to
# avoid blocking the event loop.


async def list_library_entries(status: str | None = None, limit: int = 500) -> list[dict]:
    """List entries in the authenticated user's AniList library: title, status,
    progress, score, and personal tags. Optionally filter to one status (WATCHING,
    COMPLETED, DROPPED, PLANNING, PAUSED, REPEATING). Scoped to the token's owning
    user's own library only."""
    user = _require_user()
    limit = max(1, min(limit, 1000))
    status_filter = status.strip().upper() if status else None

    def _query():
        where = "le.user_id = %s"
        params: list = [user["id"]]
        if status_filter:
            where += " AND le.status = %s"
            params.append(status_filter)
        params.append(limit)
        return db.fetchall(
            f"""
            SELECT a.id AS anime_id, a.title_romaji, a.title_english, a.format,
                   a.episodes, a.season, a.season_year, a.average_score,
                   le.status, le.score AS my_score, le.progress, le.repeat_count,
                   le.start_date, le.finish_date,
                   COALESCE(pn.personal_tags, '[]'::jsonb) AS personal_tags
            FROM library_entries le
            JOIN anime a ON a.id = le.anime_id
            LEFT JOIN personal_notes pn ON pn.anime_id = a.id AND pn.user_id = le.user_id
            WHERE {where}
            ORDER BY a.title_romaji
            LIMIT %s
            """,
            tuple(params),
        )

    rows = await run_in_threadpool(_query)
    return [_serialize_row(r) for r in rows]


async def list_personal_notes(anime_id: int | None = None) -> list[dict]:
    """List the authenticated user's personal-layer notes: drop reasons, custom
    tags, freeform notes, and manual watch-next priority — the layer AniList's own
    UI has no place for. One row per anime that has any personal_notes data.
    Optionally filter to a single anime_id (AniList media id)."""
    user = _require_user()

    def _query():
        where = "pn.user_id = %s"
        params: list = [user["id"]]
        if anime_id is not None:
            where += " AND pn.anime_id = %s"
            params.append(anime_id)
        return db.fetchall(
            f"""
            SELECT a.id AS anime_id, a.title_romaji, a.title_english,
                   pn.drop_reason, pn.personal_tags, pn.notes,
                   pn.watch_next_priority, pn.updated_at
            FROM personal_notes pn
            JOIN anime a ON a.id = pn.anime_id
            WHERE {where}
            ORDER BY a.title_romaji
            """,
            tuple(params),
        )

    rows = await run_in_threadpool(_query)
    return [_serialize_row(r) for r in rows]


async def list_recommendations(include_dismissed: bool = False, limit: int = 100) -> list[dict]:
    """List the authenticated user's recommendation_scores rows: candidate anime,
    match score, and the reason payload (matched genres/tags/studio, and which
    discovery path found it — similarity vs. seasonal) the recommender computed.
    By default excludes dismissed and currently-snoozed candidates, matching what
    the /recommendations page shows; set include_dismissed=true to see everything,
    including past dismissals and their dismiss_reason."""
    user = _require_user()
    limit = max(1, min(limit, 500))

    def _query():
        where = "rs.user_id = %s"
        if not include_dismissed:
            where += " AND rs.dismissed = false AND (rs.snoozed_until IS NULL OR rs.snoozed_until <= now())"
        return db.fetchall(
            f"""
            SELECT a.id AS anime_id, a.title_romaji, a.title_english, a.format,
                   a.episodes, a.genres, a.average_score,
                   rs.score, rs.reason, rs.source, rs.dismissed, rs.dismiss_reason,
                   rs.computed_at, rs.first_shown_at
            FROM recommendation_scores rs
            JOIN anime a ON a.id = rs.anime_id
            WHERE {where}
            ORDER BY rs.score DESC
            LIMIT %s
            """,
            (user["id"], limit),
        )

    rows = await run_in_threadpool(_query)
    return [_serialize_row(r) for r in rows]


async def get_stats() -> dict:
    """Aggregate stats for the authenticated user's library: status totals
    (completed/watching/dropped/planning counts), total episodes and watch time,
    completion rate, mean score, score distribution, and top genres among
    completed anime. Covers the core numbers the built-in /stats page shows — not
    every specialized breakdown there (e.g. the activity heatmap or drop-pattern
    analysis), which are considered out of scope for v1's read tools."""
    user = _require_user()

    def _query():
        totals = db.fetchone(
            """
            SELECT
                COUNT(*) FILTER (WHERE le.status = 'COMPLETED') AS completed,
                COUNT(*) FILTER (WHERE le.status = 'WATCHING')  AS watching,
                COUNT(*) FILTER (WHERE le.status = 'DROPPED')   AS dropped,
                COUNT(*) FILTER (WHERE le.status = 'PLANNING')  AS planning,
                COALESCE(SUM(le.progress), 0)                   AS total_episodes,
                COALESCE(SUM(le.progress * COALESCE(a.duration, 24)), 0) AS watch_minutes,
                ROUND(AVG(le.score) FILTER (WHERE le.score IS NOT NULL AND le.score > 0), 1) AS mean_score
            FROM library_entries le
            JOIN anime a ON a.id = le.anime_id
            WHERE le.user_id = %s
            """,
            (user["id"],),
        )
        score_rows = db.fetchall(
            "SELECT le.score::int AS score, COUNT(*) AS cnt FROM library_entries le "
            "WHERE le.score IS NOT NULL AND le.user_id = %s "
            "GROUP BY le.score ORDER BY le.score",
            (user["id"],),
        )
        genre_rows = db.fetchall(
            """
            SELECT genre, COUNT(*) AS cnt
            FROM library_entries le
            JOIN anime a ON a.id = le.anime_id,
                 jsonb_array_elements_text(a.genres) AS genre
            WHERE le.status = 'COMPLETED' AND le.user_id = %s
            GROUP BY genre ORDER BY cnt DESC LIMIT 12
            """,
            (user["id"],),
        )
        return totals, score_rows, genre_rows

    totals, score_rows, genre_rows = await run_in_threadpool(_query)

    completed = int(totals["completed"])
    dropped = int(totals["dropped"])
    watch_minutes = int(totals["watch_minutes"])
    return {
        "completed": completed,
        "watching": int(totals["watching"]),
        "dropped": dropped,
        "planning": int(totals["planning"]),
        "total_episodes": int(totals["total_episodes"]),
        "watch_hours": watch_minutes // 60,
        "watch_days": round(watch_minutes / 1440, 1),
        "completion_rate_pct": (
            round(completed / (completed + dropped) * 100) if (completed + dropped) > 0 else None
        ),
        "mean_score": float(totals["mean_score"]) if totals["mean_score"] is not None else None,
        "score_distribution": [{"score": r["score"], "count": r["cnt"]} for r in score_rows],
        "top_genres": [{"genre": r["genre"], "count": r["cnt"]} for r in genre_rows],
    }


_TOOL_FUNCTIONS = (list_library_entries, list_personal_notes, list_recommendations, get_stats)


# ── ASGI app + session-manager lifecycle ─────────────────────────────────────────
# asgi_app is the one long-lived object: constructed once at import time and mounted
# once in main.py (`app.mount("/mcp", mcp_server.asgi_app)`). Its target inner app is
# swapped on every start() — see _build_mcp()'s docstring for why a fresh FastMCP
# instance (and therefore a fresh, not-yet-run StreamableHTTPSessionManager) is built
# on every start() rather than reusing one across the process's lifetime.

asgi_app = _BearerAuthGate()

_current_mcp: FastMCP | None = None
_session_manager_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


async def start() -> None:
    """Builds a fresh MCP server instance, points asgi_app at it, and starts its
    session manager's background run loop — must complete before any request can
    reach asgi_app (StreamableHTTPSessionManager.handle_request asserts its task
    group is already active and raises otherwise). Called from main.py's startup
    event; see "mounting multiple FastMCP servers in a single FastAPI application",
    the advanced use case FastMCP's own session_manager property docstring names
    explicitly — this app does exactly that, just with one FastMCP instance at a
    time rather than several concurrently."""
    global _current_mcp, _session_manager_task, _stop_event

    _current_mcp = _build_mcp()
    raw_asgi_app = _current_mcp.streamable_http_app()  # also lazily creates
                                                        # _current_mcp.session_manager
    asgi_app.set_app(raw_asgi_app)

    _stop_event = asyncio.Event()
    ready_event = asyncio.Event()

    async def _run() -> None:
        async with _current_mcp.session_manager.run():
            ready_event.set()
            await _stop_event.wait()

    _session_manager_task = asyncio.create_task(_run())
    # Wait for the task group to actually be active before returning — without
    # this, FastAPI's startup event could complete (and start serving requests)
    # while _run() is merely scheduled but hasn't yet entered session_manager.run(),
    # which is exactly the not-yet-active-task-group state handle_request asserts
    # against.
    await ready_event.wait()


async def stop() -> None:
    global _session_manager_task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _session_manager_task is not None:
        await _session_manager_task
        _session_manager_task = None
    # Not reset to None here: a stray request arriving between stop() and the next
    # start() (there shouldn't be one in practice — see _BearerAuthGate's own
    # None-guard for that theoretical window) would otherwise 401 instead of
    # cleanly failing either way; leaving the now-defunct app in place doesn't
    # change which of those two "reject it" outcomes happens, just which code path
    # produces it.
