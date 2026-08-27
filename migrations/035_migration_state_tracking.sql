-- Migration 035: add migration_state, a single-row marker tracking the highest
-- migration number actually confirmed applied to THIS database (issue #380).
--
-- Confirmed live today (2026-08-27): migration 028_plex_sync_state.sql sat
-- unapplied on production for an unknown length of time (029-033 were all
-- applied, 028 wasn't) with nobody aware — only discovered by accident when
-- migration 034 failed against a missing table it depended on. There was no
-- way for the app itself, or an admin glancing at Instance Health, to know
-- prod's schema had silently fallen behind what the deployed code (`main.py`'s
-- LATEST_MIGRATION constant) expected. This table is the fix: a real signal,
-- not a manual "did I remember to run that?" check.
--
-- Deliberately detection-only, matching this issue's own scope — nothing here
-- auto-applies a migration. `scripts/mark_migration_applied.sh` is the intended
-- companion: apply the SQL file AND bump this marker in one command, so the
-- marker can't be silently forgotten the way the 028 gap itself was.
--
-- Seeded to 34, not 35 — 34 is the real, confirmed-applied state of production
-- as of right before this migration was written (034_backfill_walk_complete_flags.sql
-- was the last one actually run against prod). This migration's own number (035)
-- becomes the first real gap Instance Health will report, until someone runs
-- `scripts/mark_migration_applied.sh 35` (or applies 035 by hand and updates the
-- marker) — which is the correct, honest starting state for a tool whose whole
-- point is not silently trusting "probably fine."
--
-- Purely additive (new table + one seed row) — per CLAUDE.md's guardrail, fine to
-- run directly without special sign-off, but back up first anyway per this repo's
-- standing practice for any migration touching a live database.

BEGIN;

CREATE TABLE migration_state (
    id                          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    highest_applied_migration  INTEGER NOT NULL,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO migration_state (id, highest_applied_migration) VALUES (1, 34);

COMMIT;
