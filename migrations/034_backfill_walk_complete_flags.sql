-- Migration 034: backfill explicit walk-complete flags for Crunchyroll/Netflix/Plex
-- (issue #387).
--
-- scripts/anilist_sync_common.py's load_walk_complete() used to fall back to
-- "if sync-state rows already exist, assume the historical walk completed"
-- whenever no explicit settings row existed for a given provider — a heuristic
-- that was never safe in general, just convenient for the one moment it was
-- introduced (#97/#104): CR/Netflix/Plex users who already had real, complete
-- sync-state data from before the walk-complete flag existed at all.
--
-- Confirmed live (2026-08-26) that this fallback is a real, active bug, not just
-- theoretically unsafe: partial dev/debug runs against a live account during
-- #352's Prime Video 403 investigation wrote a few real primevideo_sync_state
-- rows before failing. The very next real sync trusted that leftover state as
-- "walk complete" via the exact same fallback, flipping full_pull from True
-- (skip-only) to False (auto-create) on a still-mostly-unwalked year of watch
-- history, and auto-created 16 bogus AniList entries in one run (14 of them
-- false-positive title matches to unrelated real anime).
--
-- The code fix removes that fallback entirely — load_walk_complete() now only
-- ever trusts an explicit settings row, full stop. This migration is the other
-- half of that fix: without it, every existing Crunchyroll/Netflix/Plex user
-- would regress to full_pull=True (skip-only, full re-walk) on their very next
-- sync, since none of them have an explicit flag row today — a real behavior
-- regression for accounts whose walk genuinely did complete under the old,
-- lucky-but-unsafe fallback. Backfilling the flag once, explicitly, for exactly
-- the users who already have real state preserves their current (correct)
-- behavior going forward.
--
-- Deliberately does NOT touch primevideo — its walk never genuinely completed
-- cleanly (that inconsistency is the root cause of this whole incident), so
-- after this ships it should correctly re-walk from scratch on its next run via
-- the now-fixed logic. That's the desired outcome here, not a gap.
--
-- Purely additive — new settings rows only, ON CONFLICT DO NOTHING, no ALTER, no
-- DROP, no data loss risk. Per CLAUDE.md's guardrail this does not require
-- explicit sign-off before running against production, but back up first
-- anyway per this repo's standing practice for any migration touching real data.

BEGIN;

INSERT INTO settings (user_id, key, value)
SELECT DISTINCT user_id, 'crunchyroll_walk_complete', 'true'
FROM cr_sync_state
ON CONFLICT (user_id, key) DO NOTHING;

INSERT INTO settings (user_id, key, value)
SELECT DISTINCT user_id, 'netflix_walk_complete', 'true'
FROM netflix_sync_state
ON CONFLICT (user_id, key) DO NOTHING;

INSERT INTO settings (user_id, key, value)
SELECT DISTINCT user_id, 'plex_walk_complete', 'true'
FROM plex_sync_state
ON CONFLICT (user_id, key) DO NOTHING;

COMMIT;
