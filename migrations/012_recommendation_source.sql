-- Migration 012: add recommendation_scores.source — distinguishes the seasonal
-- discovery digest (issue #13) from the original similarity-based recommendation
-- path, both of which now write into the same recommendation_scores table.
--
-- 'similarity' = AniList's per-show recommendations off what you've completed/
-- planned (the original, pre-existing path). 'seasonal' = AniList's current
-- season/year query (issue #13's "new this season" digest). Scoring is identical
-- either way — this column only drives labeling/filtering on the /recommendations
-- page (the "New this season" filter). Defaults every existing row to
-- 'similarity', which is accurate: no seasonal-sourced rows existed before this
-- migration.
--
-- Purely additive (new NOT NULL column with a default, no existing data touched)
-- — per CLAUDE.md's guardrail, additive migrations are fine to write directly,
-- but get explicit confirmation before running this against the live database.

BEGIN;

ALTER TABLE recommendation_scores
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'similarity';

ALTER TABLE recommendation_scores
    DROP CONSTRAINT IF EXISTS recommendation_scores_source_check;

ALTER TABLE recommendation_scores
    ADD CONSTRAINT recommendation_scores_source_check CHECK (source IN ('similarity', 'seasonal'));

COMMIT;
