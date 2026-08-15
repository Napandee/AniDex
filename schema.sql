-- Anime Tracker — Postgres schema
-- Design principle: AniList-sourced tables are fully rebuildable by the sync job.
-- Personal-layer tables are never touched by sync and are the only source of truth for
-- anything the user typed themselves (drop reasons, custom tags, queue priority).
--
-- Multi-user note: every personal-layer / library-relationship table is scoped to a
-- user via user_id. The `anime` table stays global — it's a shared AniList metadata
-- cache, the same show has the same genres/studios regardless of who's watching it.
-- This file reflects the target schema for a FRESH install. Upgrading a live
-- single-user database to this schema is a separate migration — see migrations/.

-- =========================================================================
-- USERS & ACCESS CONTROL
-- =========================================================================

-- One row per person who has ever logged in. auth_provider_id is that provider's
-- stable subject/user id for OAuth accounts (never the email — emails can change on
-- the provider side); for local accounts it's just the lowercased email, since email
-- IS the natural unique identifier when there's no external provider.
CREATE TABLE users (
    id                    SERIAL PRIMARY KEY,
    auth_provider         TEXT NOT NULL,              -- 'local' | 'google' | 'discord' — how this account was ORIGINALLY created; historical, not the active OAuth lookup path (see google_id/discord_id)
    auth_provider_id      TEXT NOT NULL,
    email                 TEXT NOT NULL,
    password_hash         TEXT,                        -- bcrypt hash; set whenever local login is enabled for this account
    google_id             TEXT UNIQUE,                  -- Google's stable subject id, set once this account is linked to Google (whether that was the original signup or a later explicit link)
    discord_id            TEXT UNIQUE,                  -- same, for Discord
    display_name          TEXT,
    avatar_url            TEXT,
    is_admin              BOOLEAN NOT NULL DEFAULT false,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at         TIMESTAMPTZ,
    failed_login_attempts INTEGER NOT NULL DEFAULT 0,  -- local login only; resets on success
    locked_until          TIMESTAMPTZ,                  -- set after 5 failures, cleared on success or reset
    UNIQUE (auth_provider, auth_provider_id)
);

-- App-wide admin configuration — deliberately NOT per-user (unlike `settings` below).
-- Currently just OAuth provider credentials: one Google app and one Discord app serve
-- the whole instance, not one per user. Keys: google_client_id, google_client_secret,
-- discord_client_id, discord_client_secret. A provider is only offered as a login
-- option once both its id and secret are set here (or via env var fallback).
CREATE TABLE instance_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Admin-managed allowlist. An email here may complete signup (local or OAuth) and get
-- an account; anyone else is rejected with no user row created. The first person to
-- ever log in (empty users table) bypasses this and becomes admin automatically.
CREATE TABLE invites (
    id              SERIAL PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    invited_by      INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    accepted_at     TIMESTAMPTZ,
    accepted_by     INTEGER REFERENCES users(id)
);

-- Admin-mediated password reset links (no email infrastructure — admin generates the
-- link and hands it to the user directly, matching the invite-only trust model).
CREATE TABLE password_resets (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);

-- =========================================================================
-- ANILIST-SOURCED (rebuildable — sync job upserts these, never hand-edit)
-- =========================================================================

-- One row per anime AniList knows about that appears anywhere in ANY user's data
-- (library, recommendations, or airing schedule). Global — not scoped to a user.
CREATE TABLE anime (
    id                  INTEGER PRIMARY KEY,        -- AniList media id (stable, use as FK everywhere)
    id_mal              INTEGER,                     -- MyAnimeList id, if AniList has it
    title_romaji        TEXT NOT NULL,
    title_english        TEXT,
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
    trailer_yt_id       TEXT,                        -- YouTube video ID from AniList trailer field
    external_links      JSONB DEFAULT '[]',           -- [{"site": "Crunchyroll", "url": "..."}, ...]
    streaming_episodes  JSONB DEFAULT '[]',           -- [{"title": "Ep 1", "url": "...", "site": "...", "thumbnail": "..."}]
    relations           JSONB DEFAULT '[]',           -- [{"id": 123, "title": "...", "cover": "...", "format": "TV", "relation_type": "SEQUEL"}]
    last_synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A user's AniList list entries — status, score, progress, real dates.
-- One row per (user, anime) the user has any relationship with.
CREATE TABLE library_entries (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id            INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    anilist_entry_id    INTEGER,                      -- AniList's own MediaListEntry id, for upsert matching
    status              TEXT NOT NULL,                -- WATCHING, COMPLETED, DROPPED, PLANNING, PAUSED, REPEATING
    score               NUMERIC(3,1),                 -- stored as 0-5 (half-star precision), converted from AniList on sync
    progress            INTEGER DEFAULT 0,            -- episodes watched
    repeat_count        INTEGER DEFAULT 0,             -- rewatches, from AniList's "repeat" field
    start_date          DATE,                          -- real watch-start date (from CrunchyExporter backfill)
    finish_date         DATE,
    anilist_updated_at  TIMESTAMPTZ,                  -- AniList's own last-updated, to detect drift
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, anime_id),
    UNIQUE (user_id, anilist_entry_id)
);

-- Cached upcoming episode airings for anything in ANY user's watching/planning list.
-- Global like `anime` — an airing time doesn't differ per user. Rebuilt on every sync.
CREATE TABLE airing_schedule_cache (
    id                  SERIAL PRIMARY KEY,
    anime_id            INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    episode             INTEGER NOT NULL,
    airing_at           TIMESTAMPTZ NOT NULL,
    UNIQUE (anime_id, episode)
);

-- Per-user record of which episodes have already been notified about. Notification
-- state can't live on airing_schedule_cache itself (that table is global/shared — two
-- users watching the same currently-airing show would otherwise race on one flag and
-- only the first ever gets notified).
CREATE TABLE notified_episodes (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id     INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    episode      INTEGER NOT NULL,
    notified_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, anime_id, episode)
);

-- =========================================================================
-- PERSONAL LAYER (never touched by sync — this is the reason the site exists)
-- =========================================================================

-- Everything AniList's own UI doesn't give a good home to.
CREATE TABLE personal_notes (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id            INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    drop_reason         TEXT,                          -- why it got dropped, freeform
    personal_tags       JSONB DEFAULT '[]',            -- ["watch with partner", "background watching", ...]
    notes                TEXT,                          -- general freeform notes
    watch_next_priority  INTEGER,                        -- manual override, lower = higher priority; null = no override
    anilist_id_override  INTEGER,                        -- if set, use this AniList ID for links and AniList-push ops
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, anime_id)
);

-- Output of the recommender job. Fully rebuildable, but kept as a table (not computed
-- on every page load) so scores are stable and dismissals persist between runs.
CREATE TABLE recommendation_scores (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id            INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    score                NUMERIC(5,2) NOT NULL,         -- higher = stronger match
    reason               JSONB DEFAULT '{}',            -- {"matched_genres": [...], "matched_tags": [...], "matched_studio": "..."}
    dismissed            BOOLEAN NOT NULL DEFAULT false,  -- user said "not interested" — exclude from future runs
    dismiss_reason       TEXT,                            -- optional chip: "not_interested", "already_watched", "wrong_genre", "too_long"
    computed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, anime_id)
);

-- =========================================================================
-- CRUNCHYROLL SYNC STATE (tracks last-known CR progress per user, per series)
-- Owned by sync_crunchyroll.py — never written to by the web app.
-- Keeps the per-series baseline so the sync can detect rewinds (rewatches)
-- and avoid acting on data that hasn't changed since last run.
-- last_seen_watched_at (added migration 004, issue #45) is a separate concern
-- from last_seen_episode: it's the fetch-side watermark that lets the CR API
-- client page newest-first and stop early, mirroring netflix_sync_state's same
-- column below — it plays no part in process()'s diff/rewatch logic, which
-- still runs entirely off last_seen_episode.
-- =========================================================================

CREATE TABLE cr_sync_state (
    user_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anilist_id            INTEGER NOT NULL,
    series_title          TEXT,                              -- for human readability
    last_seen_episode     INTEGER NOT NULL DEFAULT 0,
    last_seen_watched_at  TIMESTAMPTZ,
    rewatch_in_progress   BOOLEAN NOT NULL DEFAULT FALSE,
    last_synced_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, anilist_id)
);

-- =========================================================================
-- NETFLIX SYNC STATE (tracks last-known Netflix progress per user, per series)
-- Owned by sync_netflix.py — never written to by the web app.
-- Mirrors cr_sync_state's shape, but keyed on a last_seen_watched_at timestamp
-- watermark instead of last_seen_episode: Netflix's viewing-activity feed is
-- fetched incrementally (newest-first, stopping at the watermark) rather than as
-- a full history dump, so the baseline needed to drive that is a timestamp.
-- =========================================================================

CREATE TABLE netflix_sync_state (
    user_id                INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anilist_id              INTEGER NOT NULL,
    series_title            TEXT,                            -- for human readability
    last_seen_watched_at    TIMESTAMPTZ,
    rewatch_in_progress     BOOLEAN NOT NULL DEFAULT FALSE,
    last_synced_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, anilist_id)
);

-- =========================================================================
-- PER-USER SETTINGS (timezone, credentials, sync schedule — key/value for extensibility)
-- =========================================================================

CREATE TABLE settings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
-- No seed defaults here — defaults for a new user are applied in application code
-- (app/config.py DEFAULTS) rather than seeded rows, since there's no user to seed
-- them for until one exists.

CREATE TABLE sync_log (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
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
CREATE INDEX idx_library_entries_user ON library_entries(user_id);
CREATE INDEX idx_airing_schedule_airing_at ON airing_schedule_cache(airing_at);
CREATE INDEX idx_recommendation_scores_score ON recommendation_scores(user_id, score DESC) WHERE dismissed = false;
CREATE INDEX idx_anime_genres ON anime USING GIN (genres);
CREATE INDEX idx_anime_tags ON anime USING GIN (tags);
CREATE INDEX idx_invites_email ON invites(email) WHERE accepted_at IS NULL;
