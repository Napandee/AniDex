-- Migration 014: server-side session store (issue #82).
--
-- Replaces the previously stateless signed-cookie session (the cookie's entire
-- payload used to be `{"user_id": N}`, with nothing server-side to list or revoke)
-- with a real per-login row here, so Settings can show "active sessions" and let a
-- user revoke one. See app/sessions.py for the full read/write API and
-- app/main.py's get_current_user/_start_session/_end_session for how the signed
-- cookie now only ever carries an opaque `sid` token that's looked up against this
-- table — the cookie itself no longer carries any user identity.
--
-- Additive only, no changes to `users` or any other existing table — per CLAUDE.md's
-- guardrail this is fine to write directly, but still get explicit confirmation
-- before running it against the live database, per this repo's general migration
-- process.
--
-- ROLLOUT NOTE for whoever runs this: every existing logged-in user's cookie is in
-- the OLD `{"user_id": N}` shape and has no `sid` key at all. After this deploys,
-- get_current_user() looks for `sid` first — finds nothing in an old cookie, treats
-- the request as logged out, and clears it. Every currently-logged-in user gets
-- signed out ONE TIME on their next request after this ships and has to log back
-- in; there is no migration path for in-flight cookies (there's no user_id in the
-- new sessions table to backfill from an old cookie we never see server-side).
-- This is a deliberate, one-time, low-stakes cost — not a bug — see the PR
-- description for the fuller writeup.
--
-- Expiry/cleanup: sessions.expires_at is a fixed 30-day TTL set at creation (see
-- SESSION_TTL_DAYS in app/sessions.py), not a sliding window. There is no scheduled
-- cleanup job; dead rows (expired or revoked) are opportunistically deleted for a
-- user the next time THAT user starts a new session — same "lazy cleanup on next
-- login" precedent this repo already uses for `password_resets`.
--
-- Two indexes, for two different query shapes:
--   idx_sessions_user_active is partial (WHERE revoked_at IS NULL) and serves
--   list_active_sessions()'s read — only ever looks at live rows, ordered by
--   last_seen_at, so a partial index keeps it small even as dead rows pile up.
--   idx_sessions_user_id is a plain, non-partial index on user_id alone, added
--   specifically because the per-user cleanup DELETE in create_session() targets
--   rows where revoked_at IS NOT NULL — exactly what the partial index excludes.
--   Verified with EXPLAIN ANALYZE against a seeded 20k-row table: without this
--   index the cleanup DELETE's WHERE clause was a Seq Scan touching the whole
--   table (285 buffers); with it, a Bitmap Heap Scan bounded to just that user's
--   own rows (42 buffers) — and that gap widens as the table grows, since the seq
--   scan's cost scales with total table size while the indexed version doesn't.

BEGIN;

CREATE TABLE IF NOT EXISTS sessions (
    id             SERIAL PRIMARY KEY,
    session_token  TEXT NOT NULL UNIQUE,     -- opaque random value stored in the signed cookie's "sid" key
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_agent     TEXT,                      -- raw User-Agent header, best-effort — for the Settings "device" display only
    ip_address     TEXT,                      -- best-effort (X-Forwarded-For if present, else the connecting socket), truncated to 255 chars — cosmetic, not used for any access decision
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),  -- touched roughly every _LAST_SEEN_THROTTLE_MINUTES of activity, not on every single request — see resolve_session()
    expires_at     TIMESTAMPTZ NOT NULL,       -- fixed TTL from creation; a session past this point is treated as dead even if never explicitly revoked
    revoked_at     TIMESTAMPTZ                 -- set by the user's own "revoke" action, or logout (which revokes its own session), or the deactivation path; NULL = still active
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_active
    ON sessions (user_id, last_seen_at DESC)
    WHERE revoked_at IS NULL;

-- Covers the per-user cleanup DELETE in create_session() (targets revoked_at IS
-- NOT NULL rows among others) — see the comment above for why the partial index
-- above can't serve that query.
CREATE INDEX IF NOT EXISTS idx_sessions_user_id
    ON sessions (user_id);

COMMIT;
