-- Migration 018: personal access tokens for the MCP server (issue #207).
--
-- GitHub-PAT-style credential: a random token is generated server-side, shown to the
-- user exactly once at creation, and stored here only as a bcrypt hash — same standard
-- as users.password_hash and totp_recovery_codes.code_hash (see app/pat.py). There is
-- no way to look up a token by its hash directly, since bcrypt salts each hash
-- differently even for the same input — validation is a scan-and-bcrypt.checkpw over
-- every currently-active token in the table (see app/pat.py's resolve_token), the same
-- approach app/main.py's _consume_recovery_code_if_valid already uses for recovery
-- codes. That's a per-user scan there (user already known from the login session); here
-- it's necessarily instance-wide, since a bearer token arrives with no user_id attached
-- — acceptable at this app's invite-only personal-instance scale (a handful of users, a
-- handful of tokens each), not something to optimize ahead of an actual need.
--
-- Never deleted on revoke (revoked_at is set instead) — same "keep the row, flip a
-- timestamp" pattern as sessions.revoked_at, so a revoked token's create/use history
-- stays visible rather than silently disappearing.
--
-- Additive only (new table, no changes to any existing table) — per CLAUDE.md's
-- guardrail this is fine to write directly, but still get explicit confirmation before
-- running it against the live database.

BEGIN;

CREATE TABLE IF NOT EXISTS personal_access_tokens (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,               -- user-supplied label, e.g. "Claude Code"
    token_hash    TEXT NOT NULL,                -- bcrypt hash of the full token; never the raw token
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,                  -- bumped on every successful MCP request, best-effort
    revoked_at    TIMESTAMPTZ                   -- NULL = active; set by the user's own revoke action
);

-- Serves both listing a user's active tokens (Settings) and resolve_token()'s
-- instance-wide scan (which still filters WHERE revoked_at IS NULL first).
CREATE INDEX IF NOT EXISTS idx_pat_active
    ON personal_access_tokens (user_id)
    WHERE revoked_at IS NULL;

COMMIT;
