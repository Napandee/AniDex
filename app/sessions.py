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

Token storage (issue #311): the opaque token itself is never stored — only
SHA256(token) is, in `session_token_hash` (migration 030, replacing the old
plaintext `session_token` column). SHA-256 rather than bcrypt deliberately: the
token is already 256 bits of secrets.token_urlsafe entropy, so brute-forcing it
directly is infeasible and bcrypt's slow key-stretching (which exists to protect a
low-entropy human-chosen secret like a password) would just be a real performance
regression on a lookup that runs on every single authenticated request. A fast hash
still allows an indexed, deterministic `WHERE session_token_hash = %s` lookup,
unlike bcrypt which would force a full-table scan. hash_token() below is the single
place that hash is computed, so every call site (and the migration's backfill) stays
consistent.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app import db


def hash_token(token: str) -> str:
    """SHA256(token) as hex — see module docstring for why SHA-256 (fast, indexed
    lookup) rather than bcrypt (deliberately slow, per-row scan) is correct here.
    The single place this hash is computed, so create_session/resolve_session/etc.
    and migrations/030_session_token_hash.sql's backfill can never drift apart."""
    return hashlib.sha256(token.encode()).hexdigest()

SESSION_TTL_DAYS = 30
_DEAD_ROW_RETENTION_DAYS = 7
_LAST_SEEN_THROTTLE_MINUTES = 5  # see resolve_session's docstring

# Issue #230 — admin "login as user" impersonation. Deliberately short and fixed
# (not admin-configurable) per the issue's own scope note ("e.g. 15-30 minutes");
# 20 minutes is comfortably long enough for a real support-debugging session
# without leaving a wide-open window if an admin walks away mid-session. Enforced
# independently of SESSION_TTL_DAYS above via each impersonation row's own
# impersonation_expires_at column — see start_impersonation_session/resolve_session.
IMPERSONATION_TTL_MINUTES = 20

# Both user_agent and ip_address are attacker-controllable (raw headers — the
# latter via X-Forwarded-For, see app/main.py's _client_ip()) and only ever used
# cosmetically for the Settings display, so both get the same length cap before
# storage — nothing about auth depends on either value.
_USER_AGENT_MAX_LEN = 255
_IP_ADDRESS_MAX_LEN = 255


def create_session(user_id: int, user_agent: str | None, ip_address: str | None) -> str:
    """Create a new server-side session row for user_id and return its opaque token
    (what the caller should put in the signed cookie, under the "sid" key).

    Also opportunistically prunes this same user's own dead session rows older than
    _DEAD_ROW_RETENTION_DAYS — see module docstring. Scoped to user_id (not a
    table-wide sweep), and covered by idx_sessions_user_id (schema.sql /
    migrations/014_sessions.sql) — verified with EXPLAIN that this is a
    Bitmap-Heap-Scan-on-that-user's-rows delete, not a sequential scan of the whole
    table, and that it stays that way as the table grows (idx_sessions_user_active
    alone doesn't cover this query: it's a partial index over WHERE revoked_at IS
    NULL, and this DELETE specifically targets revoked_at IS NOT NULL rows too).
    """
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    db.execute(
        "INSERT INTO sessions (session_token_hash, user_id, user_agent, ip_address, expires_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (
            hash_token(token),
            user_id,
            (user_agent or "")[:_USER_AGENT_MAX_LEN],
            (ip_address or "")[:_IP_ADDRESS_MAX_LEN] or None,
            expires_at,
        ),
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
    """Return the user_id for an active (not revoked, not expired) session token, or
    None if the token doesn't resolve to a live session (unknown, revoked, or
    expired; also what every pre-#82 cookie hits, since those never had a token to
    look up at all).

    This runs on every authenticated request (get_current_user calls it once per
    request, via its own request.state cache), so it's read-mostly on purpose:
    last_seen_at is only written when it's more than _LAST_SEEN_THROTTLE_MINUTES
    stale, not unconditionally on every call. Without the throttle, what used to be
    a pure cookie-decode auth check would turn into a DB write on the hot path of
    every single page load; a 5-minute-stale last_seen_at is indistinguishable from
    a fresh one anywhere it's actually shown (Settings' "last active" column), so
    this trades a little precision for cutting that write volume drastically.

    Issue #230: a row created by start_impersonation_session() carries its own,
    much shorter impersonation_expires_at on top of the normal (30-day)
    expires_at. Once that shorter deadline passes this eagerly revokes the row
    (not just filters it out) so a stale impersonation cookie can never be
    resurrected — "session cannot outlive its time box" applies here the same way
    a revoked_at check applies to a manually-ended one.
    """
    token_hash = hash_token(token)
    row = db.fetchone(
        "SELECT user_id, last_seen_at, impersonated_by, impersonation_expires_at FROM sessions "
        "WHERE session_token_hash = %s AND revoked_at IS NULL AND expires_at > now()",
        (token_hash,),
    )
    if not row:
        return None
    if (
        row["impersonated_by"] is not None
        and row["impersonation_expires_at"] is not None
        and row["impersonation_expires_at"] <= datetime.now(timezone.utc)
    ):
        db.execute(
            "UPDATE sessions SET revoked_at = now() WHERE session_token_hash = %s",
            (token_hash,),
        )
        return None
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=_LAST_SEEN_THROTTLE_MINUTES)
    if row["last_seen_at"] < stale_before:
        db.execute(
            "UPDATE sessions SET last_seen_at = now() WHERE session_token_hash = %s",
            (token_hash,),
        )
    return row["user_id"]


def start_impersonation_session(
    admin_user_id: int, target_user_id: int, user_agent: str | None, ip_address: str | None
) -> str:
    """Create a new server-side session row for target_user_id, marked as an
    admin-initiated impersonation of it (issue #230). Returns the opaque token —
    same shape as create_session()'s — for the caller to point the signed cookie
    at.

    Deliberately a separate function rather than an extra create_session()
    parameter: an impersonation session is a different kind of thing (started by
    an admin, on behalf of the target, time-boxed in minutes via
    impersonation_expires_at rather than days) and keeping it fully separate means
    every existing create_session() call site/test is structurally guaranteed to
    never end up with impersonation fields set by accident.

    The row's own `expires_at` is still set to the normal SESSION_TTL_DAYS window
    (not IMPERSONATION_TTL_MINUTES) — the short deadline is enforced entirely via
    impersonation_expires_at inside resolve_session(), which revokes the row the
    moment that passes regardless of how far away the row's own expires_at still
    is. Doesn't run create_session()'s per-user dead-row prune — the *admin's*
    session (not the target's) is the one that will naturally prune this dead row
    later, the next time the admin's own account starts a fresh session."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.execute(
        "INSERT INTO sessions (session_token_hash, user_id, user_agent, ip_address, expires_at, "
        "impersonated_by, impersonation_expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            hash_token(token),
            target_user_id,
            (user_agent or "")[:_USER_AGENT_MAX_LEN],
            (ip_address or "")[:_IP_ADDRESS_MAX_LEN] or None,
            now + timedelta(days=SESSION_TTL_DAYS),
            admin_user_id,
            now + timedelta(minutes=IMPERSONATION_TTL_MINUTES),
        ),
    )
    return token


def get_impersonation_context(token: str) -> dict | None:
    """Read-only companion to resolve_session() (issue #230): if `token` currently
    resolves to a live impersonation session, returns
    {"admin_user_id": int, "expires_at": datetime}; otherwise None (not an
    impersonation session, or dead for any reason — unknown/revoked/expired
    token, or an impersonation session whose own shorter deadline has passed).

    Deliberately never revokes anything itself, unlike resolve_session() — by the
    time app/main.py's get_current_user calls this, it has already called
    resolve_session() for the same token first, so an expired-impersonation row is
    guaranteed already revoked (and this simply returns None for it) rather than
    this function needing to duplicate that side effect."""
    row = db.fetchone(
        "SELECT impersonated_by, impersonation_expires_at FROM sessions "
        "WHERE session_token_hash = %s AND revoked_at IS NULL AND expires_at > now() "
        "AND impersonated_by IS NOT NULL",
        (hash_token(token),),
    )
    if not row:
        return None
    if row["impersonation_expires_at"] is not None and row["impersonation_expires_at"] <= datetime.now(timezone.utc):
        return None
    return {"admin_user_id": row["impersonated_by"], "expires_at": row["impersonation_expires_at"]}


def revoke_session_by_token(token: str) -> None:
    """Revoke whichever session owns this token — used by logout, where the caller
    only has the token (from the cookie) and no need to look up its owning user_id
    first. Possessing the token is already the same proof of ownership the cookie
    itself relies on."""
    db.execute(
        "UPDATE sessions SET revoked_at = now() WHERE session_token_hash = %s AND revoked_at IS NULL",
        (hash_token(token),),
    )


def revoke_session(session_id: int, user_id: int) -> str | None:
    """Revoke one session row by id, scoped to user_id so a user can never revoke
    another user's session even by guessing/incrementing an id. Returns the revoked
    row's session_token_hash (the caller hashes its own current cookie token with
    hash_token() and compares against this to detect "you just revoked your own
    current session" and clear your local cookie too — the raw token itself is
    never stored, so this can't return it), or None if no matching active session
    existed for that user."""
    row = db.execute_returning(
        "UPDATE sessions SET revoked_at = now() "
        "WHERE id = %s AND user_id = %s AND revoked_at IS NULL "
        "RETURNING session_token_hash",
        (session_id, user_id),
    )
    return row["session_token_hash"] if row else None


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
    per-user table in this app). current_token is the caller's raw cookie value;
    it's hashed once here to compare against the stored session_token_hash to
    compute is_current, then dropped — no token or hash is ever handed back to a
    caller/template, so it can't end up rendered into a page."""
    current_hash = hash_token(current_token) if current_token else None
    rows = db.fetchall(
        "SELECT id, session_token_hash, user_agent, ip_address, created_at, last_seen_at "
        "FROM sessions WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now() "
        "ORDER BY last_seen_at DESC",
        (user_id,),
    )
    return [
        {
            "id": r["id"],
            "is_current": r["session_token_hash"] == current_hash,
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
