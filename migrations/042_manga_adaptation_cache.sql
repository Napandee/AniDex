-- Migration 042 — manga/light-novel "living integration" data layer (issue #454,
-- built on the direction decided in spike #450). Same "external-derived cache"
-- shape as filler_episode_cache/filler_sync_state (#299, migration 032): global/
-- catalog-wide (not per-user), fully rebuildable, never hand-edited, populated only
-- by scripts/sync_manga_data.py. See that script's own module docstring for the
-- AniList -> MangaDex -> MangaUpdates matching pipeline this backs.
--
-- Purely additive (two new tables only) — per CLAUDE.md's guardrail, additive
-- migrations are fine to write directly, but get explicit confirmation before
-- running this against the live database.

BEGIN;

CREATE TABLE IF NOT EXISTS manga_adaptation_cache (
    id                  SERIAL PRIMARY KEY,
    anime_id            INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    source_type         TEXT NOT NULL CHECK (source_type IN ('MANGA', 'NOVEL')),
    anilist_source_id   INTEGER,
    title               TEXT,
    status              TEXT,               -- AniList's own enum, reused directly: RELEASING/FINISHED/HIATUS/CANCELLED
    latest_chapter       INTEGER,             -- from MangaUpdates; null on an AniList-only fallback match
    latest_volume        INTEGER,
    last_release_at      TIMESTAMPTZ,         -- MangaUpdates' last_updated
    licensor_name        TEXT,                -- e.g. "VIZ", "Kodansha USA"
    licensor_url         TEXT,
    cover_image_url      TEXT,
    mangadex_id          TEXT,                -- cached so a later sync doesn't re-search every run
    mangaupdates_id      TEXT,
    match_method         TEXT NOT NULL CHECK (match_method IN ('anilist_only', 'mangadex_verified')),
    synced_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (anime_id, source_type)
);

-- Per-anime "have we checked for an adaptation, and did we find one" state —
-- exists separately from manga_adaptation_cache for the same reason
-- filler_sync_state exists separately from filler_episode_cache: "no adaptation
-- found" is a normal, common outcome with no cache row to hang a last-checked
-- timestamp off of.
CREATE TABLE IF NOT EXISTS manga_adaptation_sync_state (
    anime_id           INTEGER PRIMARY KEY REFERENCES anime(id) ON DELETE CASCADE,
    has_adaptation     BOOLEAN NOT NULL,
    last_checked_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
