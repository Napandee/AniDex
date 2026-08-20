-- Migration 023: "liked" flag on personal_notes, separate from score (issue #219).
--
-- (Originally numbered 022 during development; renumbered to 023 after
-- #226's 022_planning_availability_state.sql merged to main first — see the
-- sequential-numbering convention this repo follows for migrations/.)
--
-- Letterboxd separates a heart ("liked") from its star rating deliberately — a
-- show can be a 3-star "guilty pleasure I loved" or a 5-star "technically great
-- but not for me". AniDex currently conflates both into a single 0-5 score;
-- this adds an independent nullable boolean signal alongside it.
--
-- personal_notes over library_entries: this is a purely local personal-layer
-- concept, never pushed to AniList (the app's only AniList mutations are
-- rating/status/progress — see CLAUDE.md's guardrail on not adding a fourth),
-- and must survive personal_notes' own untouched-by-sync guarantee. Putting it
-- on library_entries (AniList-sourced, fully rebuilt by the sync job) would put
-- it at risk of being wiped on every resync unless sync learned to preserve it
-- the way recommendation_scores.dismissed already has to be — personal_notes
-- needs no such special-casing since sync never writes there at all.
--
-- Additive migration (new nullable column only) — no existing columns/data
-- touched. Safe to run directly per CLAUDE.md's guardrail, but get explicit
-- confirmation before running against the live DB regardless, per the same
-- guardrail.

BEGIN;

ALTER TABLE personal_notes ADD COLUMN favorite BOOLEAN;

COMMIT;
