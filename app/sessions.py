"""Server-side session store (issue #82).

Before this, the app used Starlette's SessionMiddleware with a signed cookie as the
ENTIRE session — the cookie payload was `{"user_id": N}` and there was nothing
server-side to list or revoke. "View/revoke active sessions" is impossible against
that shape, so this module adds a real `sessions` table underneath it.

Deliberately layered under SessionMiddleware rather than replacing it: the signed,
httponly, same-site cookie transport Starlette already provides is exactly what a
session-id cookie needs, and re-implementing cookie signing/parsing here would just
be reinventing itsdangerous for no benefit. What changes is the *payload* — the
cookie now carries only `{"sid": "<opaque token>"}` (see app/main.py's
_start_session/_end_session/get_current_user), and this module is the only place
that ever turns that token into a user_id, by looking it up here.

Every function here takes plain values (ids, header strings), not a fastapi
Request — same framework-agnostic shape as app/outbox.py and app/privacy.py, so
this can be unit-tested without spinning up the ASGI app.

Expiry/cleanup strategy (issue #82's rollout question): sessions carry a fixed
`expires_at` (SESSION_TTL_DAYS from creation, not sliding) and are also considered
dead once `revoked_at` is set. There's no separate scheduled cleanup job — dead rows
for a user are opportunistically deleted the next time that same user starts a new
session (see create_session's DELETE below), the same "lazy cleanup on next login"
this repo already accepts for `password_resets`. A dead row is kept for 7 days past
its death (expiry or revocation) before that sweep removes it, purely so a user who
looks at "who else is logged in" shortly after a revoke isn't confused by the row
already being gone — it still won't show up in list_active_sessions() (which filters
on revoked_at/expires_at directly), it just isn't hard-deleted immediately.
"""

import secrets
from datetime import datetime, timedelta, timezone

from app import db

SESSION_TTL_DAYS = 30
_DEAD_ROW_RETENTION_DAYS = 7


def create_session(user_id: int, user_agent: str | None, ip_address: str | None) -> str:
    """Create a new server-side session row for user_id and return its opaque token
    (what the caller should put in the signed cookie, under the "sid" key).

    Also opportunistically prunes this same user's own dead session rows older than
    _DEAD_ROW_RETENTION_DAYS — see module docstring. Scoped to user_id (not a
    table-wide sweep) so this stays a cheap, indexed delete even as the table grows.
    """
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    db.execute(
        "INSERT INTO sessions (session_token, user_id, user_agent, ip_address, expires_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (token, user_id, (user_agent or "")[:255], ip_address, expires_at),
    )
    # _DEAD_ROW_RETENTION_DAYS is a fixed internal constant (never user input), so
    # interpolating it straight into the interval literal is safe — psycopg2 can't
    # parameterize an INTERVAL's unit/magnitude directly.
    db.execute(
        f"DELETE FROM sessions WHERE user_id = %s AND ("
        f"  expires_at < now() - interval '{_DEAD_ROW_RETENTION_DAYS} days'"
        f"  OR (revoked_at IS NOT NULL AND revoked_at < now() - interval '{_DEAD_ROW_RETENTION_DAYS} days')"
        f")",
        (user_id,),
    )
    return token


def resolve_session(token: str) -> int | None:
    """Return the user_id for an active (not revoked, not expired) session token,
    touching last_seen_at while it's at it — or None if the token doesn't resolve to
    a live session (unknown, revoked, or expired; also what every pre-#82 cookie
    hits, since those never had a token to look up at all)."""
    row = db.execute_returning(
        "UPDATE sessions SET last_seen_at = now() "
        "WHERE session_token = %s AND revoked_at IS NULL AND expires_at > now() "
        "RETURNING user_id",
        (token,),
    )
    return row["user_id"] if row else None


def revoke_session_by_token(token: str) -> None:
    """Revoke whichever session owns this token — used by logout, where the caller
    only has the token (from the cookie) and no need to look up its owning user_id
    first. Possessing the token is already the same proof of ownership the cookie
    itself relies on."""
    db.execute(
        "UPDATE sessions SET revoked_at = now() WHERE session_token = %s AND revoked_at IS NULL",
        (token,),
    )


def revoke_session(session_id: int, user_id: int) -> str | None:
    """Revoke one session row by id, scoped to user_id so a user can never revoke
    another user's session even by guessing/incrementing an id. Returns the revoked
    row's session_token (the caller uses this to detect "you just revoked your own
    current session" and clear your local cookie too), or None if no matching active
    session existed for that user."""
    row = db.execute_returning(
        "UPDATE sessions SET revoked_at = now() "
        "WHERE id = %s AND user_id = %s AND revoked_at IS NULL "
        "RETURNING session_token",
        (session_id, user_id),
    )
    return row["session_token"] if row else None


def revoke_all_sessions(user_id: int) -> None:
    """Revoke every active session for user_id in one go — used when an admin
    deactivates an account (#85), so an already-open tab is cut off immediately
    rather than only being rejected the next time get_current_user() happens to
    re-check is_active. Deactivation already rejected a stale session lazily before
    #82 added this table; this just makes that eager instead of lazy now that
    there's something server-side to revoke."""
    db.execute(
        "UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
        (user_id,),
    )


def list_active_sessions(user_id: int, current_token: str | None) -> list[dict]:
    """Active sessions belonging to user_id only — never another user's rows, even
    though `sessions` isn't otherwise partitioned per caller (this is the one place
    that enforces it, via the WHERE clause below, same pattern as every other
    per-user table in this app). The raw session_token is looked at here just long
    enough to compute is_current and is then dropped — it's never handed back to a
    caller/template, so it can't end up rendered into a page."""
    rows = db.fetchall(
        "SELECT id, session_token, user_agent, ip_address, created_at, last_seen_at "
        "FROM sessions WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now() "
        "ORDER BY last_seen_at DESC",
        (user_id,),
    )
    return [
        {
            "id": r["id"],
            "is_current": r["session_token"] == current_token,
            "user_agent": r["user_agent"],
            "ip_address": r["ip_address"],
            "created_at": r["created_at"],
            "last_seen_at": r["last_seen_at"],
            "device": describe_device(r["user_agent"]),
        }
        for r in rows
    ]


def describe_device(user_agent: str | None) -> str:
    """Best-effort, dependency-free "Browser on OS" summary for the sessions list —
    good enough to tell a phone apart from a laptop at a glance. Not real UA
    parsing (no new dependency pulled in just for this cosmetic field); order of
    the checks matters since real UA strings are messy (e.g. Chrome's UA contains
    "Safari", Edge's contains both "Chrome" and "Safari")."""
    if not user_agent:
        return "Unknown device"

    if "iPhone" in user_agent:
        os_name = "iPhone"
    elif "iPad" in user_agent:
        os_name = "iPad"
    elif "Android" in user_agent:
        os_name = "Android"
    elif "Macintosh" in user_agent or "Mac OS X" in user_agent:
        os_name = "Mac"
    elif "Windows" in user_agent:
        os_name = "Windows"
    elif "Linux" in user_agent:
        os_name = "Linux"
    else:
        os_name = "Unknown OS"

    if "Edg/" in user_agent or "Edge" in user_agent:
        browser = "Edge"
    elif "OPR/" in user_agent or "Opera" in user_agent:
        browser = "Opera"
    elif "Firefox" in user_agent:
        browser = "Firefox"
    elif "CriOS" in user_agent or "Chrome" in user_agent:
        browser = "Chrome"
    elif "Safari" in user_agent:
        browser = "Safari"
    else:
        browser = "Unknown browser"

    return f"{browser} on {os_name}"
