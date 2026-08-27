-- Migration 041 (originally numbered 028 — see 2026-08-27 renumbering note below):
-- add plex_sync_state (issue #153). Same shape as cr_sync_state, not
-- netflix_sync_state — unlike Netflix's Falcor feed (no absolute episode ordinal,
-- see sync_netflix.py's module docstring), Plex's history items carry real
-- season/episode numbers the same way Crunchyroll's do, so progress is tracked as an
-- absolute last_seen_episode, not a delta count. See notes/2026-08-19-plex-sync-research.md
-- and scripts/sync_plex.py.
--
-- Renumbering note (2026-08-27, dead-code/cleanliness audit): this file and
-- 028_digest_last_seen.sql were both independently merged as "028" — this is the
-- collision that actually bit: 028_digest_last_seen.sql's own header already
-- warned "re-check the highest number on origin/main right before merging and
-- renumber this file if something else has already taken 028," but this file
-- (merged a day later) didn't do that check, and scripts/mark_migration_applied.sh's
-- `ls migrations/${N}_*.sql | head -1` picked the alphabetically-first match
-- (028_digest_last_seen.sql), silently skipping this one. plex_sync_state sat
-- entirely missing on prod until caught by accident during issue #380's own
-- work and fixed by hand the same day migration 035 shipped — this rename
-- closes the actual root cause, not just the symptom.
--
-- Purely additive (new table only) — per CLAUDE.md's guardrail, additive migrations are
-- fine to write directly, but get explicit confirmation before running this against the
-- live database.

BEGIN;

CREATE TABLE IF NOT EXISTS plex_sync_state (
    user_id                INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anilist_id              INTEGER NOT NULL,
    series_title            TEXT,
    last_seen_episode       INTEGER NOT NULL DEFAULT 0,
    last_seen_watched_at    TIMESTAMPTZ,
    rewatch_in_progress     BOOLEAN NOT NULL DEFAULT FALSE,
    last_synced_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, anilist_id)
);

COMMIT;
