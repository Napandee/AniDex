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
    is_active             BOOLEAN NOT NULL DEFAULT true,   -- soft deactivation (#85); false blocks login and drops any existing session
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at         TIMESTAMPTZ,
    failed_login_attempts INTEGER NOT NULL DEFAULT 0,  -- PASSWORD guesses only (login + /settings/2fa/disable re-auth) — resets on success or a password reset; see totp_failed_attempts below for the separate TOTP-code guess budget
    locked_until          TIMESTAMPTZ,                  -- set after 5 failures, cleared on success or reset
    totp_secret           TEXT,                         -- base32 TOTP secret; only set once setup is confirmed (issue #83)
    totp_enabled          BOOLEAN NOT NULL DEFAULT false,
    totp_enabled_at       TIMESTAMPTZ,
    totp_failed_attempts  INTEGER NOT NULL DEFAULT 0,   -- separate from failed_login_attempts (see column comment on that one) — this is the TOTP-code guess budget, not the password guess budget; a password reset never clears this one
    totp_locked_until     TIMESTAMPTZ,
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

-- One-time TOTP recovery/backup codes (issue #83) — the recovery mechanism for a lost
-- authenticator, so 2FA can never permanently lock an account out. Hashed with bcrypt,
-- same standard as users.password_hash — plaintext codes are shown to the user exactly
-- once, at enable time, and never stored anywhere. A NULL used_at row is still valid;
-- consuming a code sets used_at so it can't be replayed.
CREATE TABLE totp_recovery_codes (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash   TEXT NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_totp_recovery_codes_user ON totp_recovery_codes(user_id) WHERE used_at IS NULL;

-- Chronological trail of admin actions (issue #89). admin_user_id is who performed
-- the action; target_user_id is who it was taken against, when the action has a
-- specific target (reset-password, deactivate) — NULL for instance-wide actions
-- (invite, oauth-settings, privacy-defaults). Both FKs use ON DELETE SET NULL rather
-- than this schema's usual CASCADE, so the audit trail survives even if a referenced
-- user row is ever removed.
CREATE TABLE admin_audit_log (
    id              SERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    admin_user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action          TEXT NOT NULL,   -- 'invite_created' | 'oauth_settings_updated' | 'privacy_defaults_updated' | 'password_reset' | 'user_deactivated'
    target_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    detail          TEXT
);

-- Server-side session store (issue #82, migration 014). The signed session cookie
-- (Starlette's SessionMiddleware) carries only an opaque `sid` token pointing at a
-- row here — see app/sessions.py for the read/write API and app/main.py's
-- get_current_user/_start_session/_end_session for how the token is resolved on
-- every request. expires_at is a fixed TTL set at creation (SESSION_TTL_DAYS in
-- app/sessions.py), not a sliding window; there's no scheduled cleanup job, dead
-- rows for a user are opportunistically swept the next time that user starts a new
-- session (same lazy-cleanup precedent as password_resets above).
CREATE TABLE sessions (
    id             SERIAL PRIMARY KEY,
    session_token  TEXT NOT NULL UNIQUE,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_agent     TEXT,                      -- best-effort, Settings "device" display only, truncated to 255 chars
    ip_address     TEXT,                      -- best-effort, cosmetic — never used for any access decision, truncated to 255 chars
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),  -- touched roughly every 5 min of activity, not every request — see resolve_session()
    expires_at     TIMESTAMPTZ NOT NULL,
    revoked_at     TIMESTAMPTZ                 -- NULL = still active
);

-- Partial: serves list_active_sessions()'s read (only ever looks at live rows).
CREATE INDEX idx_sessions_user_active ON sessions (user_id, last_seen_at DESC) WHERE revoked_at IS NULL;
-- Plain: serves create_session()'s per-user cleanup DELETE, which specifically
-- targets revoked_at IS NOT NULL rows — exactly what the partial index above
-- excludes. Verified with EXPLAIN (see migrations/014_sessions.sql's comment).
CREATE INDEX idx_sessions_user_id ON sessions (user_id);

-- Personal access tokens for the MCP server (issue #207) — GitHub-PAT-style: a random
-- token is generated server-side, shown once at creation, and stored only as a bcrypt
-- hash (same standard as users.password_hash / totp_recovery_codes.code_hash). Bcrypt
-- salts each hash differently even for identical input, so there's no way to look a
-- token up by its hash directly — validation (app/pat.py's resolve_token) scans every
-- currently-active row and bcrypt.checkpw's each one, the same approach
-- _consume_recovery_code_if_valid already uses for recovery codes, just instance-wide
-- instead of per-user since a bearer token arrives with no user_id attached. Fine at
-- this app's invite-only personal-instance scale. Never deleted on revoke — revoked_at
-- is set instead, same pattern as sessions.revoked_at above.
--
-- scope (issue #208, migration 021): 'read' can only call the read-only MCP tools;
-- 'read_write' can additionally call the write tools (notes/bulk-tag/rating/status/
-- progress). Defaults to 'read' — the safe choice — so a token has to explicitly opt
-- into write access at creation time.
CREATE TABLE personal_access_tokens (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,               -- user-supplied label, e.g. "Claude Code"
    token_hash    TEXT NOT NULL,                -- bcrypt hash of the full token; never the raw token
    scope         TEXT NOT NULL DEFAULT 'read'
                     CHECK (scope IN ('read', 'read_write')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,                  -- bumped on every successful MCP request, best-effort
    revoked_at    TIMESTAMPTZ                   -- NULL = active; set by the user's own revoke action
);

CREATE INDEX idx_pat_active ON personal_access_tokens (user_id) WHERE revoked_at IS NULL;

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
    sync_status         TEXT NOT NULL DEFAULT 'synced' -- 'pending' while a bulk-edit outbox item is in flight;
                                                        -- guards the pull sync from clobbering it, see #18
                            CHECK (sync_status IN ('synced', 'pending')),
    UNIQUE (user_id, anime_id),
    UNIQUE (user_id, anilist_entry_id)
);

-- Outbox for local-first AniList delivery (issue #18, extended by #100) — a row here
-- means an edit has landed in library_entries (locally, sync_status='pending') but not
-- yet been confirmed pushed to AniList. Rows are deleted on successful push; only
-- in-flight or failed edits ever sit in this table. Originally UI bulk-status edits
-- only (issue #18); #100 extended it to also carry Crunchyroll/Netflix/Prime-originated
-- progress updates, so the app's single shared worker (app/outbox.py) delivers every
-- source under one collective AniList rate-limit budget instead of each provider script
-- independently making its own synchronous, blocking SaveMediaListEntry calls.
-- status/progress/repeat_count are independently nullable — a row carries whichever
-- subset of fields actually changed (a UI bulk-status edit only ever sets status; a
-- provider-sync progress advance may set only progress, or progress+status together).
CREATE TABLE status_sync_outbox (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id     INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    source       TEXT NOT NULL DEFAULT 'ui_bulk_edit'
                     CHECK (source IN ('ui_bulk_edit', 'crunchyroll', 'netflix', 'prime_video')),
    status       TEXT,
    progress     INTEGER,
    repeat_count INTEGER,
    state        TEXT NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending', 'in_progress', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status IS NOT NULL OR progress IS NOT NULL OR repeat_count IS NOT NULL)
);

CREATE INDEX idx_status_sync_outbox_state ON status_sync_outbox (state);
CREATE INDEX idx_status_sync_outbox_user_created ON status_sync_outbox (user_id, created_at);

-- Cached upcoming episode airings for anything in ANY user's watching/planning list.
-- Global like `anime` — an airing time doesn't differ per user. Rebuilt on every sync.
CREATE TABLE airing_schedule_cache (
    id                  SERIAL PRIMARY KEY,
    anime_id            INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    episode             INTEGER NOT NULL,
    airing_at           TIMESTAMPTZ NOT NULL,
    UNIQUE (anime_id, episode)
);

-- Persistent AniList title-search cache (issue #115). find_anilist_id() falls back to
-- AniList's live search API for any watched title not in a user's pre-built title
-- index — for accounts with a lot of non-anime watch history this dominates sync time,
-- and without persistence a retry re-searches the same permanently-non-matching titles
-- from scratch. Global like `anime`/airing_schedule_cache — a search result for a given
-- title string is the same regardless of which user or provider is asking.
CREATE TABLE anilist_title_search_cache (
    title      TEXT PRIMARY KEY,
    media_id   INTEGER,  -- NULL means "confirmed no AniList match"
    cached_at  TIMESTAMPTZ NOT NULL DEFAULT now()
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

-- A note attached to a specific rewatch (issue #14), separate from the general/
-- original note in personal_notes.notes. Dedicated table rather than a JSON array
-- field on personal_notes: personal_notes is otherwise flat/single-row-per-anime,
-- and this gets clean per-row upsert semantics plus the same FK integrity the other
-- personal-layer tables get. Sync jobs must never write here, only the app's own
-- note-editing routes.
CREATE TABLE rewatch_notes (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id            INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    -- Which rewatch this note is for, matching library_entries.repeat_count.
    -- 1 = first rewatch; the original watch (repeat_count 0) keeps using
    -- personal_notes.notes — no row here for that case.
    repeat_count        INTEGER NOT NULL CHECK (repeat_count >= 1),
    note                TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, anime_id, repeat_count)
);

CREATE INDEX idx_rewatch_notes_user_anime ON rewatch_notes (user_id, anime_id);

-- A note attached to a specific episode (issue #210), surfaced next to the progress
-- stepper. Same shape/rationale as rewatch_notes above — one-row-per-episode doesn't
-- fit personal_notes' flat single-row-per-anime shape. Sync jobs must never write
-- here, only the app's own episode-note routes.
CREATE TABLE episode_notes (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id            INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    -- Which episode this note is for, matching library_entries.progress. Only valid
    -- for an episode already watched — see _save_episode_note()'s range check.
    episode_number      INTEGER NOT NULL CHECK (episode_number >= 1),
    note                TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, anime_id, episode_number)
);

CREATE INDEX idx_episode_notes_user_anime ON episode_notes (user_id, anime_id);

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
    snoozed_until        TIMESTAMPTZ,                     -- time-boxed "not now" (issue #75); NULL = not snoozed.
                                                            -- Excluded from the recommendations view while in the
                                                            -- future, same as dismissed = true, but resurfaces once
                                                            -- it passes. Preserved across recommender rebuilds the
                                                            -- same way dismissed is (added migration 008).
    source               TEXT NOT NULL DEFAULT 'similarity'  -- how this candidate was discovered (issue #13):
                                                            -- 'similarity' = AniList's per-show recommendations off
                                                            -- what you've completed/planned (the original path);
                                                            -- 'seasonal' = AniList's current-season/year query (the
                                                            -- new "new this season" digest). Scoring is identical
                                                            -- either way — this only drives labeling/filtering on
                                                            -- the /recommendations page. If a candidate is
                                                            -- discovered via both paths in the same run, 'seasonal'
                                                            -- wins (see run_recommender.py's score_and_store) since
                                                            -- it's the more specific label. Added migration 012.
                             CHECK (source IN ('similarity', 'seasonal')),
    computed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_shown_at       TIMESTAMPTZ NOT NULL DEFAULT now(),  -- set once, at first insert, and
                                                            -- never touched by a rescore (issue
                                                            -- #185) — unlike computed_at, which
                                                            -- score_and_store() bumps to now() on
                                                            -- every rerun, this is the true "when
                                                            -- was this first recommended" anchor
                                                            -- the recommend->outcome hit-rate
                                                            -- window is measured from. Preserved
                                                            -- across rebuilds the same way
                                                            -- dismissed/snoozed_until are. Added
                                                            -- migration 017.
    UNIQUE (user_id, anime_id)
);

-- Manual Crunchyroll title/season -> AniList id overrides (issue #159). Personal
-- layer: never written to by sync_crunchyroll.py, only read by it (see
-- load_title_overrides() there), checked ahead of the season-suffix heuristic and
-- bare-title search. The web app owns all writes, via the "Crunchyroll title
-- overrides" section on /settings. series_title is stored lowercased/trimmed so it
-- matches CR's raw series_title.lower() exactly, the same normalization
-- find_anilist_id()/title_index already use elsewhere.
CREATE TABLE cr_title_overrides (
    id             SERIAL PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    series_title   TEXT NOT NULL,
    season_number  INTEGER NOT NULL DEFAULT 1,
    anilist_id     INTEGER NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, series_title, season_number)
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
-- STREAMING COVERAGE (issue #182) — "services I own", scored against the library by
-- episodes-remaining. See migrations/016_streaming_coverage.sql for the schema-choice
-- rationale (free TEXT service name validated against app/main.py's STREAMING_SITES
-- allowlist in application code, not a DB-level CHECK/FK).
-- =========================================================================

CREATE TABLE user_streaming_services (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service    TEXT NOT NULL,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, service)
);

-- Named, saved filter combinations over the library view's existing tag/status/
-- score/format/season/rewatch/sort controls (issue #200). `filters` is a
-- whitelisted snapshot of that client-side filter/sort state, not a list of
-- anime ids — a collection is a shortcut to a filter state, never a place an
-- anime is manually added. See app/main.py's COLLECTION_FILTER_KEYS.
CREATE TABLE collections (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    filters     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
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
    type            TEXT NOT NULL,          -- 'full_sync' | 'force_full_resync' | 'recommender' | 'netflix_csv_import'
    status          TEXT NOT NULL,          -- 'running' (transient) | 'ok' | 'partial' | 'error'
    entries_updated INTEGER,                -- sum of steps[].entries_updated across non-skipped
                                             -- steps (issue #46) — real entries touched this run,
                                             -- not a library row count
    error_msg       TEXT,
    trigger         TEXT,                   -- 'manual' | 'scheduled' (issue #46, migration 009);
                                             -- full_sync/force_full_resync only, null for recommender
    steps           JSONB                   -- full_sync only; see scripts/run_full_sync.py. Shape:
                                             -- [{"service": "crunchyroll"|"netflix"|"anilist_postgres",
                                             --   "status": "running"|"ok"|"error"|"skipped",
                                             --   "entries_updated": int|null, "error_msg": str|null,
                                             --   "full_pull": bool|null, "entries_fetched": int|null}, ...]
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
CREATE INDEX idx_collections_user ON collections (user_id);
CREATE INDEX idx_admin_audit_log_created_at ON admin_audit_log(created_at DESC);
