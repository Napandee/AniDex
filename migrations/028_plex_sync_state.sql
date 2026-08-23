-- Migration 028: add plex_sync_state (issue #153). Same shape as cr_sync_state, not
-- netflix_sync_state — unlike Netflix's Falcor feed (no absolute episode ordinal,
-- see sync_netflix.py's module docstring), Plex's history items carry real
-- season/episode numbers the same way Crunchyroll's do, so progress is tracked as an
-- absolute last_seen_episode, not a delta count. See notes/2026-08-19-plex-sync-research.md
-- and scripts/sync_plex.py.
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
