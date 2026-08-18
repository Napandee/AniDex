-- Migration 011: attach a note to a specific rewatch (issue #14) instead of it
-- overwriting or blending into the general/original note in personal_notes.notes.
--
-- Dedicated table rather than a JSON array field on personal_notes: personal_notes
-- is otherwise a flat, single-row-per-anime table, and a dedicated table gets clean
-- per-row upsert semantics (ON CONFLICT on a single rewatch note) plus the same FK
-- integrity (anime/users cascade) the other personal-layer tables get, instead of a
-- read-modify-write of a whole JSON array for every edit.
--
-- Personal layer, same category as personal_notes/recommendation_scores per the
-- repo's CLAUDE.md data model — sync jobs must never write to this table, only the
-- app's own note-editing routes.
--
-- Additive migration (new table only) — no existing columns/data touched. Safe to
-- run directly per CLAUDE.md's guardrail, but get explicit confirmation before
-- running against the live DB regardless, per the same guardrail.

BEGIN;

CREATE TABLE rewatch_notes (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id     INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    -- Which rewatch this note is for, matching library_entries.repeat_count at the
    -- time it airs/is watched. 1 = first rewatch (repeat_count 0, the original
    -- watch, keeps using personal_notes.notes — no row here for that case).
    repeat_count INTEGER NOT NULL CHECK (repeat_count >= 1),
    note         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, anime_id, repeat_count)
);

CREATE INDEX idx_rewatch_notes_user_anime ON rewatch_notes (user_id, anime_id);

COMMIT;
