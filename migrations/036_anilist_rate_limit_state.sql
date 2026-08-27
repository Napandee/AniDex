-- Migration 036: add anilist_rate_limit_state (issue #381).
--
-- Every AniList-calling script (app/outbox.py, scripts/anilist_sync_common.py's
-- gql() shared by the Crunchyroll/Netflix/Plex/Prime Video sync scripts,
-- scripts/sync_anilist.py's own gql()) already retries on a 429 using AniList's
-- own Retry-After header — that retry logic is fine and untouched by this
-- migration. What's missing is any way for an admin to SEE that it's happening
-- without reading container logs. This is a single-row marker recording the
-- most recent 429 observed, read by Admin > Instance Health (_instance_health()
-- in app/main.py) — visibility only, no behavior change.
--
-- No seed row: absence of a row means "never observed a rate limit on this
-- instance," which is the correct default for both a fresh install and an
-- upgrade — same "no row = nothing to report" contract migration 035's
-- migration_state table already established for pending-migration detection.
--
-- Purely additive (new table only) — per CLAUDE.md's guardrail, additive
-- migrations are fine to write directly, but get explicit confirmation before
-- running this against the live database.

BEGIN;

CREATE TABLE anilist_rate_limit_state (
    id                   INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    source               TEXT NOT NULL,
    retry_after_seconds  INTEGER NOT NULL,
    observed_at          TIMESTAMPTZ NOT NULL
);

COMMIT;
