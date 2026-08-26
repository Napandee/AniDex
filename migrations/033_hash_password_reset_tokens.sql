-- Migration 033: hash password_resets.token (issue #358, split from the same
-- 2026-08-26 security audit as #357).
--
-- Before this, password_resets.token stored the raw opaque reset token
-- (secrets.token_urlsafe(32), 256 bits of entropy) in plaintext as the primary key,
-- looked up via `WHERE token = %s`. app/sessions.py already fixed exactly this
-- pattern for session tokens under issue #311/migration 030
-- (session_token -> session_token_hash, SHA-256) — that fix was never propagated
-- to password_resets. A DB-read-only leak within the token's active window (1 hour)
-- was a direct, no-guessing account takeover: read the row, use the token to reset
-- the password, log in — arguably worse than a session-hijack from the same kind of
-- leak, since it mints a brand new credential rather than reusing a bounded-lifetime
-- one.
--
-- Deliberately SHA-256 via sessions.hash_token(), not a second hashing convention —
-- same reasoning as migration 030: the token is already 256 bits of random entropy,
-- so a fast indexed hash lookup is correct and bcrypt's slow key-stretching (built
-- for protecting a low-entropy human-chosen secret) would be pure overhead here.
--
-- Rollout differs from migration 030's backfill-then-drop approach: session tokens
-- back then needed "don't force every logged-in user to re-login", so 030 backfilled
-- session_token_hash from the still-readable plaintext column before dropping it.
-- password_resets has no equivalent concern — every row is a single-use, 1-hour-lived,
-- admin-generated link (see schema.sql's comment above the table), so this migration
-- simply deletes every existing row (invalidating any outstanding raw tokens, which
-- an admin can trivially regenerate via "Reset password" again) rather than
-- backfilling a hash for values about to be thrown away anyway.
--
-- Renames the primary-key column — per CLAUDE.md's guardrail this is NOT purely
-- additive. Back up first and get explicit confirmation before running this against
-- production. Do not merge the PR containing this migration without immediately
-- running it against the live database in the same coordinated step — this repo's
-- deploy pipeline auto-deploys app code on merge to main but does not auto-apply
-- schema migrations, and issue #358's app-code half of this fix (reading/writing
-- token_hash instead of token) will error on every password-reset call until this
-- migration has run.

BEGIN;

-- Every existing row is a live-or-expired single-use admin-generated link; deleting
-- them is the simplest safe way to retire the old plaintext values (no in-flight
-- reset is worth preserving across this migration — an admin can regenerate one in
-- one click via "Reset password" if genuinely needed).
DELETE FROM password_resets;

ALTER TABLE password_resets RENAME COLUMN token TO token_hash;

COMMIT;
