-- Migration 008: persistent AniList title-search cache (issue #115).
-- (2026-08-27: this number briefly collided with what's now migration 040
-- (recommendation_scores.snoozed_until) — see that file's header. This one keeps
-- its original number since it merged first, chronologically.)
-- find_anilist_id() falls back to AniList's live search API for any watched title not
-- already in a user's own pre-built title index. For accounts with a lot of non-anime
-- watch history (Western Netflix/Crunchyroll content), this search fallback dominates
-- sync time — AniList's rate limit is ~30 req/min in practice (see anilist_sync_common.py's
-- gql() comment), so a few hundred non-matching titles alone can take 15+ minutes. Without
-- persistence, every sync retry re-searches the same permanently-non-matching titles from
-- scratch, since the previous cache was purely in-process memory.
--
-- Global (not per-user): AniList's search results for a given title string are the same
-- regardless of which user or provider (Crunchyroll/Netflix/future Prime) is asking, so one
-- cached miss benefits every user's sync, not just one.
--
-- Additive only — new table, no changes to existing ones.

BEGIN;

CREATE TABLE IF NOT EXISTS anilist_title_search_cache (
    title      TEXT PRIMARY KEY,
    media_id   INTEGER,  -- NULL means "confirmed no AniList match"
    cached_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
