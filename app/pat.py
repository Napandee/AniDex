"""Personal access tokens (issue #207) — GitHub-PAT-style credentials a user issues
from Settings and hands to an external MCP client (Claude Code, a self-configured MCP
client) so it can read that user's own AniDex data over the MCP server in
app/mcp_server.py. Issue #208 added write-capable tools on top of #207's read-only
foundation, and with them a read/write scope on the token itself (see SCOPE_READ /
SCOPE_READ_WRITE below) — #207 deliberately left this open since it was moot until a
write tool existed to gate.

Storage/verification follows the same pattern app/main.py already uses for TOTP
recovery codes (see its _hash_recovery_code / _consume_recovery_code_if_valid): bcrypt
at rest, same standard as users.password_hash, and — because bcrypt salts each hash
differently even for identical input — no indexed lookup is possible, so resolve_token
scans every currently-active row and bcrypt-compares each one. Recovery codes get away
with a *per-user* scan because the user is already known (mid-login, from the session);
a bearer token arrives with no user_id attached at all, so this scan is necessarily
instance-wide. That's fine at this app's invite-only personal-instance scale (a handful
of users, a handful of tokens each) — not something to optimize ahead of an actual need.
"""

import secrets

import bcrypt

from app import db

# Recognizable prefix (same idea as GitHub's ghp_/gho_ etc.) so a token is
# identifiable at a glance in logs/config files without decoding anything — the
# random part after it is the only thing that's ever checked.
TOKEN_PREFIX = "adx_pat_"

# Read/write scope (issue #208, migration 021). SCOPE_READ can only call the
# read-only tools in mcp_server.py (list_library_entries etc.); SCOPE_READ_WRITE can
# additionally call the write tools (update_personal_notes, bulk_apply_tags,
# set_rating, set_status, set_progress). There is no write-only scope — a client
# that can write still needs to read to be useful, same as every other MCP client
# shape in this app.
SCOPE_READ = "read"
SCOPE_READ_WRITE = "read_write"
VALID_SCOPES = (SCOPE_READ, SCOPE_READ_WRITE)


def generate_token() -> str:
    """A fresh random token, shown to the caller exactly once — see main.py's
    settings_create_token, which never persists the return value anywhere except the
    one-time in-process display dict, mirroring _totp_recovery_display."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return bcrypt.hashpw(token.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def create_token(user_id: int, name: str, scope: str = SCOPE_READ) -> tuple[int, str]:
    """Generates + stores a new token for user_id, returning (row id, raw token). The
    raw token is never written to the database — only its bcrypt hash is.

    scope defaults to read-only — the safe choice per issue #208's PAT-scoping
    decision — and is only ever SCOPE_READ_WRITE when the caller (the Settings
    token-creation form) explicitly passes it. An invalid scope value silently
    falls back to read-only rather than erroring, so a malformed/unexpected form
    value can never accidentally mint a write-capable token."""
    raw = generate_token()
    scope_val = scope if scope in VALID_SCOPES else SCOPE_READ
    row = db.execute_returning(
        "INSERT INTO personal_access_tokens (user_id, name, token_hash, scope) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (user_id, name.strip()[:200], hash_token(raw), scope_val),
    )
    return row["id"], raw


def list_tokens(user_id: int) -> list[dict]:
    """Active (non-revoked) tokens for the Settings page — id/name/created_at/
    last_used_at/scope only, never the hash, and obviously never the raw token
    (which isn't stored anywhere after creation)."""
    return db.fetchall(
        "SELECT id, name, created_at, last_used_at, scope FROM personal_access_tokens "
        "WHERE user_id = %s AND revoked_at IS NULL ORDER BY created_at DESC",
        (user_id,),
    )


def revoke_token(user_id: int, token_id: int) -> bool:
    """Revokes one of user_id's own tokens. Scoped to (id, user_id) in the WHERE
    clause — same defense-in-depth shape as settings_revoke_session — so passing
    another user's token_id here just matches no row rather than needing a separate
    ownership check first."""
    row = db.execute_returning(
        "UPDATE personal_access_tokens SET revoked_at = now() "
        "WHERE id = %s AND user_id = %s AND revoked_at IS NULL RETURNING id",
        (token_id, user_id),
    )
    return row is not None


def resolve_token(raw_token: str | None) -> dict | None:
    """Validates a raw bearer token against every currently-active token in the
    table (bcrypt.checkpw per row — see module docstring for why no indexed lookup
    is possible) and, on a match, returns the owning user row plus best-effort bumps
    last_used_at. Returns None for a missing/malformed/unmatched/revoked token, or
    for a user who's since been deactivated (issue #85's is_active flag) — a
    revoked/deactivated account's PAT stops working immediately, same as its
    sessions do.

    The TOKEN_PREFIX check is a cheap short-circuit only (real security is the
    bcrypt compare below) — it just avoids doing bcrypt work at all for a request
    that's obviously not even shaped like one of our tokens.

    The returned dict carries the resolved user's own columns plus one extra key,
    `pat_scope` (SCOPE_READ or SCOPE_READ_WRITE, issue #208) — the scope of the
    specific token that was presented, not a property of the user. mcp_server.py's
    write tools check this before doing anything, so a read-only token can't reach
    them even though it resolves to a real, active user.
    """
    if not raw_token or not raw_token.startswith(TOKEN_PREFIX):
        return None

    candidate_bytes = raw_token.encode("utf-8")
    rows = db.fetchall(
        # pat.id/pat.scope are aliased explicitly (as pat_id/pat_scope) — without
        # the pat_id alias, RealDictCursor's column-name-keyed dict would silently
        # let `u.*`'s own `id` column clobber `pat.id` (both named plain "id"), and
        # the UPDATE below would then match personal_access_tokens by a user id
        # instead of a token id. pat_scope doesn't collide with any users column
        # today, but it's aliased anyway so that stays true if one is ever added.
        "SELECT pat.id AS pat_id, pat.token_hash, pat.scope AS pat_scope, u.* "
        "FROM personal_access_tokens pat "
        "JOIN users u ON u.id = pat.user_id "
        "WHERE pat.revoked_at IS NULL AND u.is_active = true"
    )
    for row in rows:
        if bcrypt.checkpw(candidate_bytes, row["token_hash"].encode("utf-8")):
            db.execute(
                "UPDATE personal_access_tokens SET last_used_at = now() WHERE id = %s",
                (row["pat_id"],),
            )
            user = dict(row)
            user.pop("token_hash", None)
            user.pop("pat_id", None)
            return user
    return None
