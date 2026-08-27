-- Migration 040 (originally numbered 008 — see 2026-08-27 renumbering note below):
-- add recommendation_scores.snoozed_until — time-boxed "not now" dismiss, separate
-- from the existing permanent `dismissed` flag. See issue #75.
--
-- Renumbering note (2026-08-27, dead-code/cleanliness audit): this file and
-- 008_anilist_title_search_cache.sql were both independently merged as "008" — a
-- real collision, one of three found (008, 009, 028), matching the exact
-- silent-skip risk that caused 028_plex_sync_state.sql to go unapplied on prod
-- until caught separately the same day (see migration 035's header for that
-- incident). scripts/mark_migration_applied.sh's `ls migrations/${N}_*.sql | head
-- -1` would silently apply only one file per number. Confirmed directly against
-- prod before renumbering: recommendation_scores.snoozed_until already exists
-- there (this migration's own content was already applied historically, just
-- tracked under a colliding number) — this rename is a no-op for any install that
-- already has it (ADD COLUMN IF NOT EXISTS), and a real apply for one that
-- doesn't.
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
