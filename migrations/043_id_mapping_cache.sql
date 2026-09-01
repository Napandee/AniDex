-- Migration 043 — AniDB/MAL -> AniList id mapping cache (issue #447), sourced
-- from Fribb/anime-lists (github.com/Fribb/anime-lists), a community-maintained,
-- auto-updated JSON dataset (confirmed live: pushes on a roughly weekly cadence,
-- 42,870 entries, no license file — a documented risk, accepted since this app
-- only reads the data, never redistributes it). Same "external-derived cache"
-- shape as filler_episode_cache/manga_adaptation_cache: global/catalog-wide (not
-- per-user, not even scoped to this app's own `anime` table — it's useful
-- precisely for titles Plex has matched that aren't in the local catalog yet),
-- fully rebuildable, never hand-edited, populated only by
-- scripts/sync_id_mappings.py. Unlike those two, there's no per-row sync-state
-- table: this dataset has no per-item "checked, no match" concept to track —
-- each weekly run replaces the whole table from the upstream file in one pass
-- (~43k rows, single JSON fetch, no per-row external API calls to rate-limit).
--
-- Consumed by scripts/sync_plex.py's Guid-based fast path: a Plex library item
-- matched by the HAMA (AniDB) or MyAnimeList.bundle agent carries an AniDB or
-- MAL id in its Guid list, resolved here directly instead of falling through to
-- title-matching. See sync_plex.py's module docstring for the exact Guid-prefix
-- formats this maps from.
--
-- Purely additive (one new table) — per CLAUDE.md's guardrail, additive
-- migrations are fine to write directly, but get explicit confirmation before
-- running this against the live database.

BEGIN;

CREATE TABLE IF NOT EXISTS anidb_mal_mapping_cache (
    anilist_id   INTEGER NOT NULL PRIMARY KEY,
    anidb_id     INTEGER,
    mal_id       INTEGER,
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Non-unique: the upstream dataset is keyed by anilist_id, but a handful of
-- anidb_id/mal_id values could in principle repeat across rows (e.g. upstream
-- data quality issues) — a lookup index shouldn't reject the sync over that,
-- it should just resolve to whichever row it finds first.
CREATE INDEX IF NOT EXISTS idx_anidb_mal_mapping_cache_anidb_id
    ON anidb_mal_mapping_cache (anidb_id) WHERE anidb_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_anidb_mal_mapping_cache_mal_id
    ON anidb_mal_mapping_cache (mal_id) WHERE mal_id IS NOT NULL;

COMMIT;
