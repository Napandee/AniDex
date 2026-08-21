-- Migration 027: admin "login as user" impersonation (issue #230).
--
-- Renumbering history — several other issues from the same 2026-08-20
-- brainstorm batch were landing in parallel, each claiming the "next" number
-- before this one could merge: originally written as 022, renumbered to 025
-- when 022-024 were claimed by #219/#223/#224's migrations, renumbered again
-- to 026 when #231 claimed 025, and finally to this file's actual number
-- (027) when #218's mood-tags migration claimed 026 right before this PR
-- opened. Re-verified against `git ls-tree origin/main:migrations` immediately
-- before each rename rather than trusting an earlier check.
--
-- Adds two nullable columns to the existing `sessions` table (issue #82,
-- migration 014) rather than a new table: an impersonation session IS a normal
-- session row (same auth path, same get_current_user resolution, same
-- is_active/deactivation checks) — it just additionally carries who started it
-- and when it auto-expires. Keeping it on `sessions` means every existing
-- session-lifecycle mechanism (revoke, dead-row cleanup, "active sessions" list
-- filtering) applies to an impersonation row for free, rather than needing a
-- parallel table with its own copy of that logic.
--
-- impersonated_by: the admin who started this impersonation session, NULL for
-- every ordinary (non-impersonation) session. ON DELETE SET NULL rather than
-- CASCADE, matching admin_audit_log's FKs above — an old impersonation row
-- shouldn't vanish just because the admin account that started it was later
-- removed (users are only ever soft-deactivated today, never hard-deleted, but
-- the column shouldn't assume that stays true forever).
--
-- impersonation_expires_at: the short (IMPERSONATION_TTL_MINUTES in
-- app/sessions.py, currently 20 minutes) time-box, enforced independently of
-- this row's own much longer `expires_at` (still the normal 30-day
-- SESSION_TTL_DAYS) — see resolve_session()'s auto-revoke-on-expiry check.
--
-- Additive only (two new nullable columns, no drops, no existing data touched)
-- — per CLAUDE.md's guardrail this is fine to run directly, but still get
-- explicit confirmation before running it against the live database.

BEGIN;

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS impersonated_by INTEGER REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS impersonation_expires_at TIMESTAMPTZ;

COMMIT;
