-- Migration 001: multi-user foundations for an EXISTING (live, single-user) database.
--
-- Purely additive: new tables, and nullable user_id columns on existing tables.
-- Safe to run against live data with zero risk of loss — nothing existing is altered
-- or dropped. Existing rows simply have user_id = NULL until migration 002 backfills
-- them once the owner has completed their first login (see migrations/002).
--
-- Idempotent: safe to re-run (IF NOT EXISTS everywhere).

CREATE TABLE IF NOT EXISTS users (
    id                    SERIAL PRIMARY KEY,
    auth_provider         TEXT NOT NULL,
    auth_provider_id      TEXT NOT NULL,
    email                 TEXT NOT NULL,
    password_hash         TEXT,
    google_id             TEXT UNIQUE,
    discord_id            TEXT UNIQUE,
    display_name          TEXT,
    avatar_url            TEXT,
    is_admin              BOOLEAN NOT NULL DEFAULT false,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at         TIMESTAMPTZ,
    failed_login_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until          TIMESTAMPTZ,
    UNIQUE (auth_provider, auth_provider_id)
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id TEXT UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS discord_id TEXT UNIQUE;

-- Backfill for accounts that originally signed up via OAuth before this column existed —
-- the account-linking feature looks up by google_id/discord_id uniformly regardless of
-- whether that identity was the original signup method or added later via /settings/link.
UPDATE users SET google_id  = auth_provider_id WHERE auth_provider = 'google'  AND google_id  IS NULL;
UPDATE users SET discord_id = auth_provider_id WHERE auth_provider = 'discord' AND discord_id IS NULL;

CREATE TABLE IF NOT EXISTS instance_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    id              SERIAL PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    invited_by      INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    accepted_at     TIMESTAMPTZ,
    accepted_by     INTEGER REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_invites_email ON invites(email) WHERE accepted_at IS NULL;

CREATE TABLE IF NOT EXISTS password_resets (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS notified_episodes (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id     INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    episode      INTEGER NOT NULL,
    notified_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, anime_id, episode)
);

ALTER TABLE library_entries       ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE personal_notes        ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE recommendation_scores ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE cr_sync_state         ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE sync_log              ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE settings              ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_library_entries_user ON library_entries(user_id);
