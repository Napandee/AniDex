-- Migration 002: backfill existing rows to the owner's new user_id, then tighten
-- constraints to match the fresh-install schema.
--
-- DO NOT RUN THIS until:
--   1. Migration 001 has been applied.
--   2. Auth is deployed and the current owner has logged in once (becomes user #1 /
--      admin via the bootstrap rule).
--   3. You know that user's id — replace :owner_id below with the real integer.
--
-- This ALTERs existing columns and constraints per CLAUDE.md's guardrail ("ask before
-- any schema migration that could drop or alter existing columns/data") — get explicit
-- confirmation before running this against live data, even though it's a straight
-- backfill with no data loss if :owner_id is correct.

BEGIN;

-- Backfill: every row currently in the (single-user) live database belongs to the owner.
UPDATE library_entries       SET user_id = :owner_id WHERE user_id IS NULL;
UPDATE personal_notes        SET user_id = :owner_id WHERE user_id IS NULL;
UPDATE recommendation_scores SET user_id = :owner_id WHERE user_id IS NULL;
UPDATE cr_sync_state         SET user_id = :owner_id WHERE user_id IS NULL;
UPDATE sync_log               SET user_id = :owner_id WHERE user_id IS NULL;
UPDATE settings                SET user_id = :owner_id WHERE user_id IS NULL;

-- Require user_id going forward.
ALTER TABLE library_entries       ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE personal_notes        ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE recommendation_scores ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE cr_sync_state         ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE sync_log              ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE settings               ALTER COLUMN user_id SET NOT NULL;

-- library_entries: global UNIQUE(anime_id) -> per-user UNIQUE(user_id, anime_id).
-- Also drop the old global UNIQUE(anilist_entry_id) (declared inline as a column
-- constraint in the original schema) in favor of the per-user composite one — AniList's
-- entry ids happen to already be globally unique, but the constraint should express our
-- own intended invariant, not depend on that.
ALTER TABLE library_entries DROP CONSTRAINT IF EXISTS library_entries_anime_id_key;
ALTER TABLE library_entries DROP CONSTRAINT IF EXISTS library_entries_anilist_entry_id_key;
ALTER TABLE library_entries ADD CONSTRAINT library_entries_user_id_anime_id_key UNIQUE (user_id, anime_id);
ALTER TABLE library_entries ADD CONSTRAINT library_entries_user_id_anilist_entry_id_key UNIQUE (user_id, anilist_entry_id);

-- personal_notes: global UNIQUE(anime_id) -> per-user UNIQUE(user_id, anime_id).
ALTER TABLE personal_notes DROP CONSTRAINT IF EXISTS personal_notes_anime_id_key;
ALTER TABLE personal_notes ADD CONSTRAINT personal_notes_user_id_anime_id_key UNIQUE (user_id, anime_id);

-- recommendation_scores: global UNIQUE(anime_id) -> per-user UNIQUE(user_id, anime_id).
ALTER TABLE recommendation_scores DROP CONSTRAINT IF EXISTS recommendation_scores_anime_id_key;
ALTER TABLE recommendation_scores ADD CONSTRAINT recommendation_scores_user_id_anime_id_key UNIQUE (user_id, anime_id);

-- cr_sync_state: PK was (anilist_id) -> PK becomes (user_id, anilist_id).
ALTER TABLE cr_sync_state DROP CONSTRAINT IF EXISTS cr_sync_state_pkey;
ALTER TABLE cr_sync_state ADD PRIMARY KEY (user_id, anilist_id);

-- settings: PK was (key) -> PK becomes (user_id, key).
ALTER TABLE settings DROP CONSTRAINT IF EXISTS settings_pkey;
ALTER TABLE settings ADD PRIMARY KEY (user_id, key);

-- recommendation_scores index needs user_id added — old version only sorted globally.
DROP INDEX IF EXISTS idx_recommendation_scores_score;
CREATE INDEX idx_recommendation_scores_score ON recommendation_scores(user_id, score DESC) WHERE dismissed = false;

-- airing_schedule_cache.notified is dead now that notification state lives per-user in
-- notified_episodes (see migration 001) — it was a single global flag per (anime,
-- episode) that caused a real bug: two users watching the same currently-airing show
-- would race on it, and only whichever user's loop iteration ran first got notified.
DROP INDEX IF EXISTS idx_airing_schedule_airing_at;
ALTER TABLE airing_schedule_cache DROP COLUMN IF EXISTS notified;
CREATE INDEX idx_airing_schedule_airing_at ON airing_schedule_cache(airing_at);

COMMIT;
