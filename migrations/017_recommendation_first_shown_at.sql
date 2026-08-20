-- Migration 017: add recommendation_scores.first_shown_at — issue #185's
-- recommend->outcome hit-rate instrumentation.
--
-- (Originally numbered 016 during development; renumbered to 017 after #182's
-- Streaming Coverage migration merged to main and claimed 016 first — see the
-- sequential-numbering convention this repo follows for migrations/.)
--
-- `computed_at` is overwritten to now() on every recommender rerun (see
-- score_and_store()'s ON CONFLICT DO UPDATE), so it only ever reflects the *last*
-- time a candidate was rescored, not when the user first saw it recommended. A
-- candidate that keeps surfacing week after week would otherwise look like it was
-- "just recommended" forever, making any recommend->outcome window comparison
-- meaningless for anything but a brand-new candidate.
--
-- first_shown_at is the fix: set once, at first insert, and preserved across
-- reruns the same way `dismissed`/`dismiss_reason`/`snoozed_until` already are —
-- run_recommender.py's score_and_store() ON CONFLICT DO UPDATE deliberately never
-- assigns it. See CLAUDE.md's "recommendation_scores is rebuilt ... but must
-- preserve the dismissed flag across rebuilds" guarantee — first_shown_at now
-- carries the same guarantee, for the same reason.
--
-- Backfilled to the existing `computed_at` for every pre-existing row — an
-- imperfect but reasonable stand-in (it's the earliest timestamp already on
-- record for a row that predates this column; the alternative, NULL, would just
-- exclude every historical recommendation from the hit-rate metric entirely
-- until it happens to get rescored and re-inserted, which never happens since
-- ON CONFLICT rows are never deleted/reinserted).
--
-- Purely additive (new NOT NULL column with a default, existing rows backfilled
-- rather than dropped/altered destructively) — per CLAUDE.md's guardrail,
-- additive migrations are fine to write directly, but get explicit confirmation
-- before running this against the live database.

BEGIN;

ALTER TABLE recommendation_scores ADD COLUMN IF NOT EXISTS first_shown_at TIMESTAMPTZ;

UPDATE recommendation_scores SET first_shown_at = computed_at WHERE first_shown_at IS NULL;

ALTER TABLE recommendation_scores ALTER COLUMN first_shown_at SET DEFAULT now();
ALTER TABLE recommendation_scores ALTER COLUMN first_shown_at SET NOT NULL;

COMMIT;
