-- Migration 030: hash sessions.session_token (issue #311, split from the #309
-- security audit).
--
-- Before this, sessions.session_token stored the raw opaque session token
-- (secrets.token_urlsafe(32), 256 bits of entropy) in plaintext, looked up via
-- `WHERE session_token = %s` on every authenticated request. A DB leak made every
-- currently-active session instantly usable — the exact asymmetry
-- personal_access_tokens.token_hash already avoided for PATs.
--
-- Deliberately SHA-256, not bcrypt (see the issue body / app/sessions.py's module
-- docstring for the full reasoning): the token is already 256 bits of random
-- entropy, so brute-forcing it directly is infeasible — bcrypt's slow
-- key-stretching exists to protect a low-entropy *human-chosen* secret (a
-- password, or a PAT scanned per-row since PAT validation is a low-frequency MCP
-- call). Sessions are validated on every single web request; a full-table bcrypt
-- scan at that frequency would be a real, measurable performance regression. A
-- fast SHA-256 hash still supports an indexed, deterministic
-- `WHERE session_token_hash = %s` lookup — no functional downgrade, same
-- single-row query shape as before, just no longer plaintext at rest.
--
-- Rollout: existing active sessions must keep working, not force every logged-in
-- user to re-login. That's possible here (unlike, say, a case where only a
-- one-way hash of the *client's* secret exists) because the plaintext
-- session_token is still fully readable server-side right up until this
-- migration runs — so this backfills session_token_hash = SHA256(session_token)
-- for every existing row BEFORE dropping the plaintext column, in the same
-- transaction. Postgres core (14+; this repo runs postgres:16-alpine, see
-- compose/*.yml and .github/workflows/pr-validate.yml) ships sha256(bytea) ->
-- bytea built in, no pgcrypto extension needed.
--
-- Additive-data-then-drop, not a pure additive migration (drops the old
-- session_token column at the end) — per CLAUDE.md's guardrail this needs
-- explicit confirmation before running against live data, even though the
-- backfill itself is safe (hashing loses no information needed anywhere else;
-- there's no key to manage, unlike e.g. #310's credential encryption). Do not run
-- this against production without that confirmation.

BEGIN;

-- 1. Add the new column, nullable for now — populated by the backfill below
--    before being tightened to NOT NULL.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS session_token_hash TEXT;

-- 2. Backfill every existing row from its still-readable plaintext token, so
--    already-active sessions resolve correctly the moment app/sessions.py cuts
--    its lookups over to session_token_hash — no forced re-login.
UPDATE sessions
SET session_token_hash = encode(sha256(session_token::bytea), 'hex')
WHERE session_token_hash IS NULL;

-- 3. Now that every row is backfilled, enforce the same NOT NULL + UNIQUE shape
--    the old session_token column had (idx name matches schema.sql's fresh-install
--    constraint naming for a column-level UNIQUE).
ALTER TABLE sessions ALTER COLUMN session_token_hash SET NOT NULL;
ALTER TABLE sessions ADD CONSTRAINT sessions_session_token_hash_key UNIQUE (session_token_hash);

-- 4. Drop the old plaintext column (and, implicitly, its UNIQUE constraint/index)
--    now that nothing reads or writes it — per the issue's acceptance criteria,
--    the two columns must not sit side by side holding equivalent information
--    indefinitely.
ALTER TABLE sessions DROP COLUMN session_token;

COMMIT;
