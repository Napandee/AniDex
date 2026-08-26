-- Migration 032: fix status_sync_outbox's source CHECK constraint (issues #17, #153).
--
-- migrations/010_provider_sync_outbox.sql's original constraint was
-- CHECK (source IN ('ui_bulk_edit', 'crunchyroll', 'netflix', 'prime_video')) — a
-- speculative placeholder for Prime Video added before #17 was implemented, using
-- the wrong key spelling ('prime_video' with an underscore) and missing 'plex'
-- entirely. Every actual call site — sync_primevideo.py's enqueue_outbox_update(conn,
-- anilist_id, "primevideo", ...) and sync_plex.py's identical "plex" call — uses the
-- no-underscore/plain spelling, matching every other reference to these providers
-- throughout the codebase (run_full_sync.py's _run_step(..., "primevideo", ...) /
-- (..., "plex", ...), scripts/*_sync_state tables, etc.).
--
-- Confirmed live in production (2026-08-26): the very first real Prime Video sync
-- that resolved a genuinely new AniList entry (issue #252's create-vs-skip path)
-- hit this exact constraint — "new row for relation status_sync_outbox violates
-- check constraint status_sync_outbox_source_check" — aborting that row's
-- transaction. Plex has never hit this yet only because no user on this instance
-- has connected Plex; the bug is identical and latent there too.
--
-- Alters an existing constraint on a live table — per CLAUDE.md's guardrail, this
-- is NOT purely additive. Back up first and get explicit confirmation before running
-- this against the live database.

BEGIN;

ALTER TABLE status_sync_outbox DROP CONSTRAINT status_sync_outbox_source_check;
ALTER TABLE status_sync_outbox ADD CONSTRAINT status_sync_outbox_source_check
    CHECK (source IN ('ui_bulk_edit', 'crunchyroll', 'netflix', 'plex', 'primevideo'));

COMMIT;
