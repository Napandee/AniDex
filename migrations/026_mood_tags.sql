-- Migration 026: mood tags on personal_notes (issue #218).
--
-- (Originally numbered 022 during development; renumbered to 024 after
-- #226's 022_planning_availability_state.sql and #219's 023_favorite_flag.sql
-- both merged to main first; renumbered again to 025 after #220's
-- 024_episode_notes_memorable_quote.sql also merged first; renumbered again
-- to 026 after #231's 025_invite_expiry.sql also merged first — see the
-- sequential-numbering convention this repo follows for migrations/, same
-- renumbering note as 023's own header.)
--
-- StoryGraph-inspired: let a user tag a personal note with a mood ("comfort
-- watch", "intense", "sad", "hype", ...) at the point they log it, alongside
-- the existing freeform personal_tags. Implemented as a dedicated JSONB array
-- column rather than folded into personal_tags — mood is a closed, app-defined
-- picklist (app/main.py's MOOD_TAGS), unlike personal_tags which is arbitrary
-- freeform text, so keeping it separate means a future mood chart/filter
-- (explicitly out of scope for #218 itself, but the natural follow-up once
-- this data exists) can group on it directly without having to guess which
-- personal_tags entries were "really" moods.
--
-- No DB-level CHECK constraint against the picklist: mood_tags is a JSONB
-- array, not a scalar column, so a simple CHECK (... IN (...)) doesn't apply
-- the way it did for personal_access_tokens.scope in migration 021. Validated
-- in application code instead (app/main.py's MOOD_TAGS allowlist), same
-- pattern already used for user_streaming_services.service against
-- STREAMING_SITES (see CLAUDE.md's Data Model section) — unrecognized values
-- submitted directly against the JSON API are silently dropped rather than
-- rejected, since the UI itself only ever offers checkboxes for the fixed set.
--
-- Additive only (new column with a safe default, no drops, no existing data
-- touched) — per CLAUDE.md's guardrail this is fine to write directly, but
-- still get explicit confirmation before running it against the live database.

BEGIN;

ALTER TABLE personal_notes
    ADD COLUMN IF NOT EXISTS mood_tags JSONB DEFAULT '[]';

COMMIT;
