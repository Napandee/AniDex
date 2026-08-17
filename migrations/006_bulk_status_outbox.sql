-- Migration 006: local-first bulk status edits with async AniList sync (issue #18).
-- Adds a dirty-flag guard column to library_entries so the AniList pull sync
-- (scripts/sync_anilist.py's upsert_library_entry()) never clobbers a local edit
-- that hasn't reached AniList yet, plus an outbox table a background worker drains
-- to push those edits. Additive only — get explicit confirmation before running
-- against the live DB, per CLAUDE.md.

BEGIN;

ALTER TABLE library_entries
    ADD COLUMN IF NOT EXISTS sync_status TEXT NOT NULL DEFAULT 'synced'
        CHECK (sync_status IN ('synced', 'pending'));

CREATE TABLE IF NOT EXISTS status_sync_outbox (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id     INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    status       TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending', 'in_progress', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_status_sync_outbox_state ON status_sync_outbox (state);
CREATE INDEX IF NOT EXISTS idx_status_sync_outbox_user_created
    ON status_sync_outbox (user_id, created_at);

COMMIT;
