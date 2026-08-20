-- Migration 022: per-user Planning-list streaming-availability tracking (issue #229).
--
-- Spun out of #22 (Streaming Service Coverage brainstorm). Notifies a user when a
-- title sitting in their Planning list with zero AniList `externalLinks` streaming
-- entries gains its first one — poll+diff against AniList's own community-curated
-- data on the regular sync, same category of change as the existing sync jobs'
-- watermark-diff pattern (cr_sync_state/netflix_sync_state) applied to availability
-- instead of watch progress. No new external dependency.
--
-- had_availability is tracked per (user_id, anime_id) rather than derived fresh each
-- sync from some other stored snapshot: `anime.external_links` itself is global/
-- rebuilt-in-place on every sync (no history), so there is nothing else to diff
-- against without a dedicated row here — same reasoning notified_episodes already
-- uses to justify its own existence instead of a flag on the global
-- airing_schedule_cache table (see schema.sql's comment there).
--
-- Scoped per-user, not just per-anime: coverage state is meaningful only in the
-- context of "is this on THIS user's Planning list", and two users can have the same
-- anime in different statuses (or not at all) at the same time.
--
-- Additive only (new table, no drops, no existing data touched) — per CLAUDE.md's
-- guardrail this is fine to write directly, but still get explicit confirmation
-- before running it against the live database.

BEGIN;

CREATE TABLE IF NOT EXISTS planning_availability_state (
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id          INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    had_availability  BOOLEAN NOT NULL,
    notified_at       TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, anime_id)
);

COMMIT;
