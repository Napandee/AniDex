-- Issue #186 — MMR diversity re-rank for the recommender. Purely additive: one
-- new nullable column on an existing table, nothing existing changes shape and
-- no existing value is rewritten.
--
-- Why a separate column instead of folding diversity into `score`:
-- `score` is what the recommend->outcome hit-rate (issue #185) is measured off
-- and what the UI renders as the match percentage. The baseline this change is
-- validated against (22 hits / 1173 eligible = 1.88%, captured 2026-09-10) was
-- taken on that column, so diluting it with a diversity term would both destroy
-- the before/after comparison the issue requires and desynchronise `score` from
-- the `reason` JSONB that explains it. Relevance and presentation order are
-- different concerns and get different columns.
--
-- Deliberately left NULL for every existing row. diversity_rank is only
-- populated when run_recommender.py next runs, and the read path orders by
-- `diversity_rank ASC NULLS LAST, score DESC` — so applying this migration on
-- its own is a no-op for what users see, and the ordering changes only once
-- real ranks exist. That keeps the schema change and the behaviour change
-- separable if the re-rank ever needs backing out.
ALTER TABLE recommendation_scores
    ADD COLUMN diversity_rank INTEGER;

COMMENT ON COLUMN recommendation_scores.diversity_rank IS
    'Issue #186: 1-based MMR presentation order within a recommender run. NULL '
    'until the next run populates it. Never folded into score - see migration 045.';
