-- Migration 024: add an optional favorite-quote / memorable-scene field to
-- episode_notes (issue #220). Surfaced repeatedly in a 2026-08-20 competing-
-- tracker brainstorm (publicly-shared Notion anime-tracker templates) as a
-- lightweight way to capture what made a specific episode memorable, beyond
-- a plain freeform notes field. Independent of the existing `note` column —
-- a row can carry either field alone or both; see _save_episode_note() in
-- app/main.py for the combined delete-when-both-blank logic.
--
-- (Originally numbered 022 during development; renumbered to 023 after #229's
-- Planning-list streaming-availability migration merged to main and claimed
-- 022 first, then renumbered again to 024 after #219's favorite-flag migration
-- independently claimed 023 in the same window — see the sequential-numbering
-- convention this repo follows for migrations/, same as 019's own renumbering
-- note.)
--
-- Personal layer, same category as personal_notes/rewatch_notes/episode_notes
-- itself per the repo's CLAUDE.md data model — sync jobs must never write to
-- this table, only the app's own episode-note routes.
--
-- Additive migration (new nullable column only, no existing columns/data
-- touched) — safe to run directly per CLAUDE.md's guardrail, but get explicit
-- confirmation before running against the live DB regardless, per the same
-- guardrail.

BEGIN;

ALTER TABLE episode_notes ADD COLUMN memorable_quote TEXT;

COMMIT;
