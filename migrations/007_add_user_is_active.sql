-- Migration 007: add users.is_active — soft-deactivation flag for the admin panel.
-- See issue #85: invite-only instances had no way to revoke a user's access short
-- of direct database surgery. Enforced at login (app/main.py auth_login_submit /
-- _resolve_or_create_user) and on every subsequent request (get_current_user drops
-- the session for a deactivated user), so an existing session is rejected on its
-- next request too, not just blocked at the login form.
--
-- Purely additive (new NOT NULL column with a DEFAULT, so existing rows backfill
-- automatically) — per CLAUDE.md's guardrail, additive migrations are fine to write
-- directly, but get explicit confirmation before running this against the live
-- database.

BEGIN;

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true;

COMMIT;
