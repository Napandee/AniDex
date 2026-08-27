-- Migration 039 (originally numbered 009 — see 2026-08-27 renumbering note below):
-- add admin_audit_log — chronological trail of admin actions. See issue #89.
-- Purely additive (new table), safe to run directly per CLAUDE.md's guardrail on
-- additive migrations.
--
-- Renumbering note (2026-08-27, dead-code/cleanliness audit): this file and
-- 009_add_sync_log_trigger.sql were both independently merged as "009" — a real
-- collision, one of three found (008, 009, 028), matching the exact silent-skip
-- risk that caused 028_plex_sync_state.sql to go unapplied on prod until caught
-- separately the same day (see migration 035's header for that incident).
-- scripts/mark_migration_applied.sh's `ls migrations/${N}_*.sql | head -1` would
-- silently apply only one file per number. Confirmed directly against prod before
-- renumbering: admin_audit_log already exists there (this migration's own content
-- was already applied historically, just tracked under a colliding number) — this
-- rename is a no-op for any install that already has it (CREATE TABLE IF NOT
-- EXISTS), and a real apply for one that doesn't.
--
-- admin_user_id is the admin who performed the action; target_user_id is the user
-- the action was taken against, when the action has one (reset-password, deactivate) —
-- NULL for instance-wide actions (invite, oauth-settings, privacy-defaults). Both FKs
-- use ON DELETE SET NULL rather than this schema's usual CASCADE, since an audit
-- trail should survive even if a referenced user row is ever removed — users are only
-- ever soft-deactivated today (never hard-deleted), but the log shouldn't silently
-- vanish rows if that ever changes.

BEGIN;

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id              SERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    admin_user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action          TEXT NOT NULL,   -- 'invite_created' | 'oauth_settings_updated' | 'privacy_defaults_updated' | 'password_reset' | 'user_deactivated'
    target_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    detail          TEXT
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_log_created_at ON admin_audit_log(created_at DESC);

COMMIT;
