-- Migration 004: add cr_sync_state.last_seen_watched_at, mirroring
-- netflix_sync_state's timestamp watermark. Crunchyroll's watch-history sync
-- (scripts/sync_crunchyroll.py) moves from a full page-1 walk on every run to an
-- incremental fetch that pages newest-first and stops at this watermark — the same
-- pattern netflix_sync_state already drives for sync_netflix.py. See issue #45.
--
-- Purely additive (new nullable column only) — per CLAUDE.md's guardrail, additive
-- migrations are fine to write directly, but get explicit confirmation before
-- running this against the live database.

BEGIN;

ALTER TABLE cr_sync_state ADD COLUMN IF NOT EXISTS last_seen_watched_at TIMESTAMPTZ;

COMMIT;
