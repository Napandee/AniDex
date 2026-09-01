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
    digest_last_seen_at   TIMESTAMPTZ,                  -- issue #235 — "what's new since your last visit" in-app
                                                          -- digest watermark. NULL means the digest has never run for
                                                          -- this account yet (bootstrap: record a baseline, show
                                                          -- nothing — same first-check-no-notify shape as
                                                          -- planning_availability_state, so a brand-new/pre-existing
                                                          -- user never gets their entire history dumped in one
                                                          -- banner). Deliberately NOT reused from last_login_at above
                                                          -- — that column is overwritten on every login before a
                                                          -- digest would ever get a chance to read the *previous*
                                                          -- value, and the digest can also advance on an ordinary
                                                          -- page load (not just login), which last_login_at never
                                                          -- does.
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
    accepted_by     INTEGER REFERENCES users(id),
    -- Issue #231: expiry + revoke for the admin invite flow. expires_at
    -- defaults to 7 days out (INVITE_EXPIRY_DAYS in app/main.py); a signup
    -- only succeeds against a row with accepted_at IS NULL, revoked_at IS
    -- NULL, AND expires_at > now() (see _resolve_or_create_user). See
    -- migrations/025_invite_expiry.sql for the upgrade path on an
    -- already-running instance.
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '7 days'),
    revoked_at      TIMESTAMPTZ
);

-- Admin-mediated password reset links (no email infrastructure — admin generates the
-- link and hands it to the user directly, matching the invite-only trust model).
--
-- token_hash (issue #358, migration 033): SHA256(token) hex digest via
-- sessions.hash_token(), never the raw opaque token itself — same rationale/pattern
-- as sessions.session_token_hash (issue #311, migration 030): a DB-read-only leak
-- within the token's 1-hour window would otherwise be a direct, no-guessing account
-- takeover.
CREATE TABLE password_resets (
    token_hash  TEXT PRIMARY KEY,
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
    action          TEXT NOT NULL,   -- 'invite_created' | 'oauth_settings_updated' | 'privacy_defaults_updated' | 'password_reset' | 'user_deactivated' | 'impersonation_started' | 'impersonation_ended' | 'impersonation_action' (#230)
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
--
-- session_token_hash (issue #311, migration 030): SHA256(token) hex digest, never
-- the raw opaque token itself — closes the gap where a DB leak made every
-- currently-active session instantly usable. SHA-256, not bcrypt: the token is
-- already 256 bits of secrets.token_urlsafe entropy (infeasible to brute-force
-- directly), and this column is looked up on EVERY authenticated request, so a
-- fast, indexed, deterministic `WHERE session_token_hash = %s` lookup is correct
-- here — bcrypt's slow key-stretching exists to protect a low-entropy human secret
-- (see personal_access_tokens.token_hash below for that case) and would be a real
-- performance regression at this lookup frequency. See app/sessions.py's
-- hash_token() — the single place this hash is computed. Replaces the old
-- plaintext `session_token TEXT NOT NULL UNIQUE` column, dropped in the same
-- migration once existing rows were backfilled.
CREATE TABLE sessions (
    id                  SERIAL PRIMARY KEY,
    session_token_hash  TEXT NOT NULL UNIQUE,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_agent          TEXT,                      -- best-effort, Settings "device" display only, truncated to 255 chars
    ip_address          TEXT,                      -- best-effort, cosmetic — never used for any access decision, truncated to 255 chars
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),  -- touched roughly every 5 min of activity, not every request — see resolve_session()
    expires_at          TIMESTAMPTZ NOT NULL,
    revoked_at          TIMESTAMPTZ,                -- NULL = still active
    -- Admin "login as user" impersonation (issue #230, migration 027). NULL/NULL
    -- for every ordinary session. impersonated_by is the admin who started this
    -- session; impersonation_expires_at is its own short deadline
    -- (IMPERSONATION_TTL_MINUTES in app/sessions.py), enforced independently of
    -- (and much sooner than) expires_at above — see resolve_session().
    impersonated_by           INTEGER REFERENCES users(id) ON DELETE SET NULL,
    impersonation_expires_at  TIMESTAMPTZ
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
                     CHECK (source IN ('ui_bulk_edit', 'crunchyroll', 'netflix', 'plex', 'primevideo')),
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

-- Per-user "did this Planning-list title have any streaming availability the last
-- time we checked" state (issue #229) — poll+diff over `anime.external_links` (itself
-- global/rebuilt-in-place on every sync, no history of its own) on each regular
-- AniList sync. When had_availability flips false -> true, the user gets a one-time
-- "now streaming" notification via the existing dispatcher (#51); flipping true ->
-- false (a link disappearing again) is tracked too so a later re-gain still counts as
-- a fresh transition, matching the "diff on each sync" framing rather than a
-- fire-once-ever ledger. Per-user like notified_episodes, for the same reason: two
-- users can have the same anime in different (or no) list status at once, and this is
-- only meaningful in the context of "is this on THIS user's Planning list."
CREATE TABLE planning_availability_state (
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id          INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    had_availability  BOOLEAN NOT NULL,
    notified_at       TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, anime_id)
);

-- Filler/canon episode data cache from AniFillerPedia (issue #299), a separate
-- first-party project keying its own `series` table by anilist_id — the same id
-- anime.id already is, so anime_id here is a direct FK, no fuzzy matching. Global/
-- catalog-scoped like airing_schedule_cache, not per-user: filler status doesn't
-- vary by who's watching. Populated by scripts/sync_filler_data.py, never
-- hand-edited. See migrations/029_filler_data_cache.sql for the full design
-- rationale (three tables, why filler_sync_state can't just live on the cache
-- table, why filler_data_license is a singleton).
CREATE TABLE filler_episode_cache (
    id                     SERIAL PRIMARY KEY,
    anime_id               INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    episode_number         INTEGER NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('canon', 'filler', 'mixed')),
    status_note            TEXT,
    citation_url           TEXT,
    citation_description   TEXT,
    synced_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (anime_id, episode_number)
);

-- Per-anime "have we checked AniFillerPedia for this title, and did it match" state
-- — exists separately from filler_episode_cache because "no match" and "matched,
-- zero episodes researched" are both expected, common outcomes with no cache rows to
-- hang a last-checked timestamp off of. afp_series_id NULL = no match found (yet).
CREATE TABLE filler_sync_state (
    anime_id           INTEGER PRIMARY KEY REFERENCES anime(id) ON DELETE CASCADE,
    afp_series_id      INTEGER,
    last_checked_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Single-row cache of AniFillerPedia's GET /license response, so a future UI issue
-- can render CC BY-NC-SA attribution without a live call per page render.
CREATE TABLE filler_data_license (
    id                    INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    license_name          TEXT NOT NULL,
    attribution_notice    TEXT NOT NULL,
    raw_response          JSONB NOT NULL,
    fetched_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Manga/light-novel "living integration" data cache (issue #454, spike #450) —
-- same external-derived-cache shape as filler_episode_cache/filler_sync_state just
-- above: global/catalog-scoped, not per-user, populated only by
-- scripts/sync_manga_data.py, never hand-edited. See that script's own module
-- docstring for the AniList -> MangaDex -> MangaUpdates matching pipeline and
-- migrations/042_manga_adaptation_cache.sql for the full design rationale.
CREATE TABLE manga_adaptation_cache (
    id                  SERIAL PRIMARY KEY,
    anime_id            INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    source_type         TEXT NOT NULL CHECK (source_type IN ('MANGA', 'NOVEL')),
    anilist_source_id   INTEGER,
    title               TEXT,
    status              TEXT,
    latest_chapter       INTEGER,
    latest_volume        INTEGER,
    last_release_at      TIMESTAMPTZ,
    licensor_name        TEXT,
    licensor_url          TEXT,
    cover_image_url      TEXT,
    mangadex_id          TEXT,
    mangaupdates_id      TEXT,
    match_method         TEXT NOT NULL CHECK (match_method IN ('anilist_only', 'mangadex_verified')),
    synced_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (anime_id, source_type)
);

-- Per-anime "have we checked for an adaptation, and did we find one" state — see
-- filler_sync_state's own comment above for why this can't just live on the cache
-- table (a "no adaptation found" outcome has no cache row to hang a last-checked
-- timestamp off of).
CREATE TABLE manga_adaptation_sync_state (
    anime_id           INTEGER PRIMARY KEY REFERENCES anime(id) ON DELETE CASCADE,
    has_adaptation     BOOLEAN NOT NULL,
    last_checked_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- AniDB/MAL -> AniList id mapping cache (issue #447), sourced from
-- Fribb/anime-lists. Global, not scoped to the local `anime` table (useful for
-- titles Plex has matched that aren't in the catalog yet) — no per-row
-- sync-state table, since a weekly run just replaces the whole thing in one
-- pass from a single upstream JSON file. See
-- migrations/043_id_mapping_cache.sql and scripts/sync_id_mappings.py.
CREATE TABLE anidb_mal_mapping_cache (
    anilist_id   INTEGER NOT NULL PRIMARY KEY,
    anidb_id     INTEGER,
    mal_id       INTEGER,
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_anidb_mal_mapping_cache_anidb_id
    ON anidb_mal_mapping_cache (anidb_id) WHERE anidb_id IS NOT NULL;
CREATE INDEX idx_anidb_mal_mapping_cache_mal_id
    ON anidb_mal_mapping_cache (mal_id) WHERE mal_id IS NOT NULL;

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
    mood_tags           JSONB DEFAULT '[]',            -- ["comfort", "intense", ...] — issue #218, StoryGraph-
                                                         -- inspired mood-at-log-time. Dedicated column rather than
                                                         -- folded into personal_tags: mood is a closed, app-defined
                                                         -- picklist (validated against app/main.py's MOOD_TAGS
                                                         -- allowlist, same pattern as STREAMING_SITES) so a future
                                                         -- mood chart/filter (explicitly out of scope for #218
                                                         -- itself) can group on it directly, unlike personal_tags
                                                         -- which is arbitrary freeform text.
    notes                TEXT,                          -- general freeform notes
    watch_next_priority  INTEGER,                        -- manual override, lower = higher priority; null = no override
    anilist_id_override  INTEGER,                        -- if set, use this AniList ID for links and AniList-push ops
    -- Letterboxd's heart-vs-star pattern (issue #219): a personal favorite signal
    -- independent of library_entries.score, so "3-star guilty pleasure I loved" and
    -- "5-star technically great but not for me" can both be expressed. Nullable —
    -- NULL and FALSE both read as "not favorited"; never pushed to AniList (this
    -- app's only AniList mutations are rating/status/progress, see CLAUDE.md).
    favorite             BOOLEAN,
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
    -- Optional favorite-quote / memorable-scene text (issue #220) — independent
    -- of `note` above, so a row can carry either field alone or both.
    memorable_quote     TEXT,
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
-- PLEX SYNC STATE (issue #153) — tracks last-known Plex progress per user, per
-- series. Owned by sync_plex.py — never written to by the web app.
-- Same shape as cr_sync_state, not netflix_sync_state: Plex's history items carry
-- real season/episode numbers the same way Crunchyroll's do (see
-- notes/2026-08-19-plex-sync-research.md), so progress is an absolute
-- last_seen_episode, not a delta count.
-- =========================================================================

CREATE TABLE plex_sync_state (
    user_id                INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anilist_id              INTEGER NOT NULL,
    series_title            TEXT,                            -- for human readability
    last_seen_episode       INTEGER NOT NULL DEFAULT 0,
    last_seen_watched_at    TIMESTAMPTZ,
    rewatch_in_progress     BOOLEAN NOT NULL DEFAULT FALSE,
    last_synced_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, anilist_id)
);

-- =========================================================================
-- PRIME VIDEO SYNC STATE (issue #17) — tracks last-known Prime Video progress per
-- user, per series. Owned by sync_primevideo.py — never written to by the web app.
-- Same shape as cr_sync_state/plex_sync_state, not netflix_sync_state: Prime Video's
-- watch-history API returns an exact "Episode N: <title>" string per watched episode
-- (confirmed live, see notes/2026-08-14-netflix-prime-sync-research.md), so progress
-- is tracked as an absolute last_seen_episode, not a delta count.
-- =========================================================================

CREATE TABLE primevideo_sync_state (
    user_id                INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anilist_id              INTEGER NOT NULL,
    series_title            TEXT,                            -- for human readability
    last_seen_episode       INTEGER NOT NULL DEFAULT 0,
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

-- Single-row marker: highest migration number actually confirmed applied to this
-- database (issue #380, migration 035). Compared against app/main.py's
-- LATEST_MIGRATION constant on Admin > Instance Health to surface a real "the
-- deployed code expects a migration that hasn't been run here yet" warning —
-- confirmed live to be a real, silent-failure-prone gap (migration 028 sat
-- unapplied on prod for a while with nobody aware). No seed row here, unlike
-- filler_data_license's own singleton pattern this mirrors structurally —
-- schema.sql has no precedent for seed INSERTs at all, and a fresh install has
-- nothing pending by definition, so app/main.py's read side treats a missing
-- row the same as "nothing to warn about" rather than needing one. See
-- scripts/mark_migration_applied.sh, which upserts this row on first real use
-- regardless of whether one already exists.
CREATE TABLE migration_state (
    id                          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    highest_applied_migration  INTEGER NOT NULL,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Scheduled automatic backups (issue #372, migration 037) — each row is one
-- full-instance backup zip (same content admin_export_all builds on demand),
-- stored in Postgres rather than on the app container's own filesystem, since
-- the app container has no persistent volume mounted for arbitrary files today
-- (only Postgres does). Pruned to the most recent N rows on each scheduled run.
CREATE TABLE instance_backups (
    id            SERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    size_bytes    INTEGER NOT NULL,
    user_count    INTEGER NOT NULL,
    content       BYTEA NOT NULL
);

-- Issue #381 — single-row marker for the most recently observed AniList 429,
-- read by Admin > Instance Health. No seed row: absence means "never observed
-- a rate limit," same no-row-is-fine contract as migration_state above.
CREATE TABLE anilist_rate_limit_state (
    id                   INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    source               TEXT NOT NULL,
    retry_after_seconds  INTEGER NOT NULL,
    observed_at          TIMESTAMPTZ NOT NULL
);

-- Issue #377 — one row per browser/device a user has subscribed to Web Push from.
-- The VAPID keypair signing every push lives in instance_config (see app/vapid.py),
-- not here — this table only holds what each individual subscription needs.
CREATE TABLE push_subscriptions (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint    TEXT NOT NULL,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, endpoint)
);

-- =========================================================================
-- INDEXES
-- =========================================================================

CREATE INDEX idx_instance_backups_created_at ON instance_backups (created_at DESC);
CREATE INDEX idx_push_subscriptions_user ON push_subscriptions (user_id);

CREATE INDEX idx_library_entries_status ON library_entries(status);
CREATE INDEX idx_library_entries_user ON library_entries(user_id);
CREATE INDEX idx_airing_schedule_airing_at ON airing_schedule_cache(airing_at);
CREATE INDEX idx_recommendation_scores_score ON recommendation_scores(user_id, score DESC) WHERE dismissed = false;
CREATE INDEX idx_anime_genres ON anime USING GIN (genres);
CREATE INDEX idx_anime_tags ON anime USING GIN (tags);
CREATE INDEX idx_invites_email ON invites(email) WHERE accepted_at IS NULL AND revoked_at IS NULL;
CREATE INDEX idx_collections_user ON collections (user_id);
CREATE INDEX idx_admin_audit_log_created_at ON admin_audit_log(created_at DESC);
