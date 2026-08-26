-- Migration 031: add primevideo_sync_state (issue #17). Same shape as
-- plex_sync_state/cr_sync_state, not netflix_sync_state — Prime Video's watch-history
-- API returns an exact "Episode N: <title>" string per watched episode (confirmed live,
-- see notes/2026-08-14-netflix-prime-sync-research.md's "Prime Video endpoint —
-- CONFIRMED" section), so progress is tracked as an absolute last_seen_episode, not a
-- delta count the way Netflix's sync has to. See scripts/sync_primevideo.py.
--
-- Purely additive (new table only) — per CLAUDE.md's guardrail, additive migrations are
-- fine to write directly, but get explicit confirmation before running this against the
-- live database.

BEGIN;

CREATE TABLE IF NOT EXISTS primevideo_sync_state (
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
