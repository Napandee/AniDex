-- Migration 008: add recommendation_scores.snoozed_until — time-boxed "not now"
-- dismiss, separate from the existing permanent `dismissed` flag. See issue #75.
--
-- A snoozed recommendation is excluded from the recommendations view while
-- snoozed_until is in the future, the same way `dismissed = true` already excludes
-- a row — but unlike `dismissed`, it resurfaces automatically once the timestamp
-- passes. Nullable: NULL means "not snoozed" (the default for every existing row
-- and every freshly-scored candidate).
--
-- Preserved across recommender rebuilds the same way `dismissed` already is —
-- run_recommender.py's score_and_store() ON CONFLICT DO UPDATE only ever sets
-- score/reason/computed_at, so this column (like dismissed/dismiss_reason) is never
-- touched by a rebuild. See CLAUDE.md's "recommendation_scores is rebuilt ... but
-- must preserve the dismissed flag across rebuilds" guarantee — snoozed_until now
-- carries the same guarantee.
--
-- Purely additive (new nullable column, no default needed) — per CLAUDE.md's
-- guardrail, additive migrations are fine to write directly, but get explicit
-- confirmation before running this against the live database.

BEGIN;

ALTER TABLE recommendation_scores ADD COLUMN IF NOT EXISTS snoozed_until TIMESTAMPTZ;

COMMIT;
