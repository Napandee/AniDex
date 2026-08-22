-- Migration 029: filler/canon episode data cache from AniFillerPedia (issue #299).
--
-- Foundational data-layer piece for the three filler-UI concepts filed alongside it
-- (#300/#301/#302) — this migration and scripts/sync_filler_data.py are the only
-- things those three PRs will build on top of, so the table shapes here are meant to
-- be stable before any of them land.
--
-- AniFillerPedia (github.com/Napandee/AniFillerPedia) is a separate, first-party
-- project keying its own `series` table by `anilist_id` — the same id AniDex's
-- `anime.id` already is, so anime_id here is a direct FK, no separate mapping table.
-- Its read API is public/unauthenticated: GET /series?anilist_id={id}, then
-- GET /series/{series_id}/episodes.
--
-- Three tables, same category as airing_schedule_cache/anilist_title_search_cache
-- per CLAUDE.md's data model ("AniList-sourced... fully rebuildable by the sync
-- job") even though the source here is AniFillerPedia, not AniList itself — same
-- shape: global/catalog-scoped (not per-user), fully rebuildable, never hand-edited,
-- never written to by anything except scripts/sync_filler_data.py.
--
-- 1. filler_episode_cache — one row per (anime_id, episode_number) with a match AND
--    at least one researched episode. Column names mirror AniFillerPedia's own
--    /episodes response shape directly (status/status_note/citation.url/
--    citation.description flattened onto citation_url/citation_description) so the
--    sync script's mapping is a direct field copy, no reinterpretation.
--
-- 2. filler_sync_state — one row per anime.id ever checked, independent of whether a
--    match was found. This is what answers "have we checked this title recently" —
--    it can't live on filler_episode_cache itself, because a title with no
--    AniFillerPedia match (or a matched series with zero researched episodes so far)
--    has no cache rows to hang a last-checked timestamp off of, and both of those are
--    expected steady-state outcomes for most of the catalog for a long time
--    (AniFillerPedia's own README describes its dataset as still early-stage).
--    afp_series_id is nullable and doubles as the match/no-match flag: NULL means
--    "no series match found (yet)", non-NULL means "matched, may or may not have
--    researched episodes". The sync script re-checks a NULL row on a longer cadence
--    than a matched-but-thin row, since a matched series gaining newly-researched
--    episodes is a more likely near-term event than an unmatched title suddenly
--    getting a community-proposed series entry — see sync_filler_data.py's
--    RECHECK_INTERVAL_* constants for the actual cadence, tuned independently of this
--    migration so it can change without a schema change.
--
-- 3. filler_data_license — single-row cache of GET /license's response (license name
--    + attribution notice + the full raw payload), so a future UI issue can render
--    CC BY-NC-SA attribution without a live call on every page render, matching this
--    app's existing "cache externally-sourced data, don't call live per page render"
--    principle. Singleton via a CHECK-constrained id, same shape convention as this
--    schema already uses for CHECK-enforced enum-like columns elsewhere (e.g.
--    personal_access_tokens.scope, status_sync_outbox.state).
--
-- Additive only — three new tables, no existing table/column touched. Fine to apply
-- directly per CLAUDE.md's guardrail, but this is NOT applied to the live database as
-- part of landing this migration file; that's a separate, deliberate step at
-- review/merge time.

BEGIN;

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

CREATE TABLE filler_sync_state (
    anime_id           INTEGER PRIMARY KEY REFERENCES anime(id) ON DELETE CASCADE,
    afp_series_id      INTEGER,  -- AniFillerPedia's own series.id; NULL = no match found (yet)
    last_checked_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE filler_data_license (
    id                    INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    license_name          TEXT NOT NULL,
    attribution_notice    TEXT NOT NULL,
    raw_response          JSONB NOT NULL,
    fetched_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
