-- Migration 028: users.digest_last_seen_at (issue #235).
--
-- (Sequential migrations convention — see 023/026's own header notes on
-- renumbering: three other issues, #288/#236/#283, are being built in parallel
-- worktrees at the time this was written and may also claim a migration
-- number first. Re-check the highest number on origin/main right before
-- merging and renumber this file if something else has already taken 028.)
--
-- (2026-08-27: this warning turned out to be prophetic — 028_plex_sync_state.sql
-- merged a day later without re-checking, collided with this file, and the
-- collision caused a real silent gap on prod (plex_sync_state never applied
-- until caught by accident). That file is now migration 041 — see its header
-- for the incident writeup. This one keeps its original number since it merged
-- first, chronologically.)
--
-- Watermark for the in-app "what's new since your last visit" digest (#235) —
-- a passive summary of new episodes aired for Watching-status shows, new
-- recommendations generated, and recent sync activity, shown once per
-- return visit. Deliberately in-app only, no Telegram/Discord/ntfy push (see
-- #235's own scope decision) — a separate surface from the existing per-event
-- dispatcher (#51/app/notify.py), not a second delivery channel for the same
-- alerts.
--
-- NULL means the digest has never run for this account — bootstrapped as a
-- baseline-only, no-digest-shown first pass (same shape as
-- planning_availability_state's first-check-records-baseline convention from
-- #229), so an existing user doesn't get their entire history dumped into one
-- banner the first time this ships. Not reused from the existing
-- last_login_at column: that's overwritten on every login before a digest
-- could ever read its previous value, and this watermark can also advance on
-- an ordinary page load, not just login.
--
-- Additive only (new nullable column, no default, no existing data touched)
-- — per CLAUDE.md's guardrail this is fine to write directly, but still get
-- explicit confirmation before running it against the live database.

BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS digest_last_seen_at TIMESTAMPTZ;

COMMIT;
