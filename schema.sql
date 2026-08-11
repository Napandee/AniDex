-- Anime Tracker — Postgres schema
-- Design principle: AniList-sourced tables are fully rebuildable by the sync job.
-- Personal-layer tables are never touched by sync and are the only source of truth for
-- anything the user typed themselves (drop reasons, custom tags, queue priority).

-- =========================================================================
-- ANILIST-SOURCED (rebuildable — sync job upserts these, never hand-edit)
-- =========================================================================

-- One row per anime AniList knows about that appears anywhere in the user's data
-- (library, recommendations, or airing schedule).
CREATE TABLE anime (
    id                  INTEGER PRIMARY KEY,        -- AniList media id (stable, use as FK everywhere)
    id_mal              INTEGER,                     -- MyAnimeList id, if AniList has it
    title_romaji        TEXT NOT NULL,
    title_english       TEXT,
    title_native         TEXT,
    format              TEXT,                        -- TV, MOVIE, OVA, ONA, SPECIAL
    status              TEXT,                        -- FINISHED, RELEASING, NOT_YET_RELEASED, CANCELLED
    episodes            INTEGER,                     -- total episode count, null if unknown/ongoing
    duration            INTEGER,                     -- average episode length in minutes
    season              TEXT,                        -- WINTER, SPRING, SUMMER, FALL
    season_year         INTEGER,
    genres              JSONB DEFAULT '[]',           -- ["Action", "Drama", ...]
    tags                JSONB DEFAULT '[]',           -- [{"name": "Time Travel", "rank": 87}, ...]
    studios             JSONB DEFAULT '[]',           -- [{"name": "MAPPA", "isMain": true}, ...]
    average_score       INTEGER,                     -- AniList community score, 0-100
    cover_image_url     TEXT,
    banner_image_url    TEXT,
    description         TEXT,
    external_links      JSONB DEFAULT '[]',           -- [{"site": "Crunchyroll", "url": "..."}, ...]
    streaming_episodes  JSONB DEFAULT '[]',           -- [{"title": "Ep 1", "url": "...", "site": "...", "thumbnail": "..."}]
    relations           JSONB DEFAULT '[]',           -- [{"id": 123, "title": "...", "cover": "...", "format": "TV", "relation_type": "SEQUEL"}]
    last_synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The user's AniList list entries — status, score, progress, real dates.
-- One row per anime the user has any relationship with (watching/completed/dropped/planning).
CREATE TABLE library_entries (
    id                  SERIAL PRIMARY KEY,
    anime_id            INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    anilist_entry_id    INTEGER UNIQUE,               -- AniList's own MediaListEntry id, for upsert matching
    status              TEXT NOT NULL,                -- WATCHING, COMPLETED, DROPPED, PLANNING, PAUSED, REPEATING
    score               NUMERIC(3,1),                 -- stored as 0-5 (half-star precision), converted from AniList on sync
    progress            INTEGER DEFAULT 0,            -- episodes watched
    repeat_count        INTEGER DEFAULT 0,             -- rewatches, from AniList's "repeat" field
    start_date          DATE,                          -- real watch-start date (from CrunchyExporter backfill)
    finish_date         DATE,
    anilist_updated_at  TIMESTAMPTZ,                  -- AniList's own last-updated, to detect drift
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (anime_id)                                 -- one library entry per anime
);

-- Cached upcoming episode airings for anything in the user's watching/planning list.
-- Rebuilt on every sync; used to drive the "new episode" notification workflow in n8n.
CREATE TABLE airing_schedule_cache (
    id                  SERIAL PRIMARY KEY,
    anime_id            INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    episode             INTEGER NOT NULL,
    airing_at           TIMESTAMPTZ NOT NULL,
    notified            BOOLEAN NOT NULL DEFAULT false,  -- flips true once n8n has sent the alert
    UNIQUE (anime_id, episode)
);

-- =========================================================================
-- PERSONAL LAYER (never touched by sync — this is the reason the site exists)
-- =========================================================================

-- Everything AniList's own UI doesn't give a good home to.
CREATE TABLE personal_notes (
    id                  SERIAL PRIMARY KEY,
    anime_id            INTEGER NOT NULL UNIQUE REFERENCES anime(id) ON DELETE CASCADE,
    drop_reason         TEXT,                          -- why it got dropped, freeform
    personal_tags       JSONB DEFAULT '[]',            -- ["watch with partner", "background watching", ...]
    notes                TEXT,                          -- general freeform notes
    watch_next_priority  INTEGER,                        -- manual override, lower = higher priority; null = no override
    anilist_id_override  INTEGER,                        -- if set, use this AniList ID for links and AniList-push ops
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Output of the recommender job. Fully rebuildable, but kept as a table (not computed
-- on every page load) so scores are stable and dismissals persist between runs.
CREATE TABLE recommendation_scores (
    id                  SERIAL PRIMARY KEY,
    anime_id            INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    score                NUMERIC(5,2) NOT NULL,         -- higher = stronger match
    reason               JSONB DEFAULT '{}',            -- {"matched_genres": [...], "matched_tags": [...], "matched_studio": "..."}
    dismissed            BOOLEAN NOT NULL DEFAULT false,  -- user said "not interested" — exclude from future runs
    dismiss_reason       TEXT,                            -- optional chip: "not_interested", "already_watched", "wrong_genre", "too_long"
    computed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (anime_id)
);

-- =========================================================================
-- CRUNCHYROLL SYNC STATE (tracks last-known CR progress per series)
-- Owned by sync_crunchyroll.py — never written to by the web app.
-- Keeps the per-series baseline so the sync can detect rewinds (rewatches)
-- and avoid acting on data that hasn't changed since last run.
-- =========================================================================

CREATE TABLE IF NOT EXISTS cr_sync_state (
    anilist_id            INTEGER PRIMARY KEY,
    series_title          TEXT,                              -- for human readability
    last_seen_episode     INTEGER NOT NULL DEFAULT 0,
    rewatch_in_progress   BOOLEAN NOT NULL DEFAULT FALSE,
    last_synced_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =========================================================================
-- APP SETTINGS (single-user preferences — key/value for extensibility)
-- =========================================================================

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Seed defaults (idempotent)
INSERT INTO settings (key, value) VALUES ('timezone',              'Europe/London') ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('sync_daily_time',       '04:30')         ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('sync_recommender_day',  'sun')           ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('sync_recommender_time', '05:00')         ON CONFLICT (key) DO NOTHING;
-- Credential keys: no default values — must be set via Settings page or .env fallback
INSERT INTO settings (key, value) VALUES ('anilist_username',    '') ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('anilist_token',       '') ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('cr_etp_rt',           '') ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('telegram_bot_token',  '') ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('telegram_chat_id',    '') ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS sync_log (
    id              SERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    type            TEXT NOT NULL,          -- 'full_sync' | 'recommender'
    status          TEXT NOT NULL,          -- 'ok' | 'error'
    entries_updated INTEGER,
    error_msg       TEXT
);

-- =========================================================================
-- INDEXES
-- =========================================================================

CREATE INDEX idx_library_entries_status ON library_entries(status);
CREATE INDEX idx_airing_schedule_airing_at ON airing_schedule_cache(airing_at) WHERE notified = false;
CREATE INDEX idx_recommendation_scores_score ON recommendation_scores(score DESC) WHERE dismissed = false;
CREATE INDEX idx_anime_genres ON anime USING GIN (genres);
CREATE INDEX idx_anime_tags ON anime USING GIN (tags);
