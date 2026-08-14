-- Migration 003: add netflix_sync_state, mirroring cr_sync_state's shape but keyed on
-- a last_seen_watched_at timestamp watermark instead of last_seen_episode — Netflix's
-- viewingactivity feed is timestamp-ordered and fetched incrementally (newest-first,
-- stopping at the watermark), so the state needed to drive that is a timestamp, not an
-- episode count. See scripts/sync_netflix.py and issue #48.
--
-- Purely additive (new table only) — per CLAUDE.md's guardrail, additive migrations are
-- fine to write directly, but get explicit confirmation before running this against the
-- live database.

BEGIN;

CREATE TABLE IF NOT EXISTS netflix_sync_state (
    user_id                INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anilist_id              INTEGER NOT NULL,
    series_title            TEXT,
    last_seen_watched_at    TIMESTAMPTZ,
    rewatch_in_progress     BOOLEAN NOT NULL DEFAULT FALSE,
    last_synced_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, anilist_id)
);

COMMIT;
