-- Migration 004: add a `steps` JSONB column to sync_log, so a full_sync run's per-step
-- detail (crunchyroll / netflix / anilist_postgres, [prime_video later, see #49]) is
-- recorded instead of only ever one flat status for the whole pipeline. Also widens
-- sync_log.status to a tri-state ('ok' | 'partial' | 'error'), plus a transient
-- 'running' value written at the start of a run — see scripts/run_full_sync.py and
-- issue #62.
--
-- Purely additive (new nullable column) — per CLAUDE.md's guardrail, additive
-- migrations are fine to write directly, but get explicit confirmation before running
-- this against the live database.

BEGIN;

ALTER TABLE sync_log ADD COLUMN IF NOT EXISTS steps JSONB;

COMMIT;
