-- Migration 019: attach a note to a specific episode (issue #210), matching the
-- exact shape issue #14's rewatch_notes already established for the same kind of
-- one-row-per-X problem — personal_notes is a flat, single-row-per-anime table,
-- and this is fundamentally one-row-per-episode.
--
-- (Originally numbered 018 during development; renumbered to 019 after #211's
-- Collections migration merged to main and claimed 018 first — see the
-- sequential-numbering convention this repo follows for migrations/, same as
-- 017's own renumbering note.)
--
-- Personal layer, same category as personal_notes/rewatch_notes/recommendation_scores
-- per the repo's CLAUDE.md data model — sync jobs must never write to this table,
-- only the app's own episode-note routes.
--
-- Additive migration (new table only) — no existing columns/data touched. Safe to
-- run directly per CLAUDE.md's guardrail, but get explicit confirmation before
-- running against the live DB regardless, per the same guardrail.

BEGIN;

CREATE TABLE episode_notes (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id        INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    -- Which episode this note is for, matching library_entries.progress at the
    -- time the note is written. Only valid for an episode already watched — see
    -- _save_episode_note()'s range check in app/main.py.
    episode_number  INTEGER NOT NULL CHECK (episode_number >= 1),
    note            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, anime_id, episode_number)
);

CREATE INDEX idx_episode_notes_user_anime ON episode_notes (user_id, anime_id);

COMMIT;
