-- Migration 010: extend status_sync_outbox to also carry Crunchyroll/Netflix/Prime-
-- originated AniList updates (issue #100), not just UI bulk-status edits (issue #18).
-- Adds `source` (observability: which origin queued this row), and independently
-- nullable `progress`/`repeat_count` columns alongside the now-nullable `status` — a
-- row carries whichever subset of fields actually changed. Additive only (new/relaxed
-- columns, no drops) — safe to run directly per CLAUDE.md's guardrail, but get explicit
-- confirmation before running against the live DB regardless, per the same guardrail.

BEGIN;

ALTER TABLE status_sync_outbox
    ALTER COLUMN status DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'ui_bulk_edit',
    ADD COLUMN IF NOT EXISTS progress INTEGER,
    ADD COLUMN IF NOT EXISTS repeat_count INTEGER;

ALTER TABLE status_sync_outbox
    DROP CONSTRAINT IF EXISTS status_sync_outbox_source_check,
    ADD CONSTRAINT status_sync_outbox_source_check
        CHECK (source IN ('ui_bulk_edit', 'crunchyroll', 'netflix', 'prime_video'));

ALTER TABLE status_sync_outbox
    DROP CONSTRAINT IF EXISTS status_sync_outbox_has_a_field_check,
    ADD CONSTRAINT status_sync_outbox_has_a_field_check
        CHECK (status IS NOT NULL OR progress IS NOT NULL OR repeat_count IS NOT NULL);

COMMIT;
