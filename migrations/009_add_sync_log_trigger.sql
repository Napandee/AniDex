-- Migration 009: add a `trigger` column to sync_log (issue #46), so the sync-history
-- UI can distinguish a manual "Sync Now"/"Force Full Resync" click from the daily
-- APScheduler-driven run — today's `type` column only ever says 'full_sync' or
-- 'force_full_resync', neither of which says who/what kicked the run off.
--
-- Threaded from app/main.py's _run_sync_task (defaults to 'manual' for both sync
-- routes; 'scheduled' passed explicitly from _scheduled_full_sync) through a TRIGGER
-- env var into scripts/run_full_sync.py's _start_log().
--
-- Additive only — new nullable column, no changes to existing ones. Per CLAUDE.md's
-- guardrail, additive migrations are fine to write directly, but get explicit
-- confirmation before running this against the live database.

BEGIN;

ALTER TABLE sync_log ADD COLUMN IF NOT EXISTS trigger TEXT;

COMMIT;
