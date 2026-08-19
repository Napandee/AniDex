-- Migration 013: Crunchyroll season-aware matching — manual override table (issue #159).
--
-- Personal-layer table (see CLAUDE.md's data-model categories): never written to by
-- any sync job. sync_crunchyroll.py only ever reads it (load_title_overrides()), to
-- resolve a (series_title, season_number) pair straight to an anilist_id before
-- falling back to the season-suffix heuristic / bare-title search. The web app owns
-- all writes, via the new "Crunchyroll title overrides" section on /settings.
-- series_title is stored lowercased/trimmed by the app so it matches CR's raw
-- series_title.lower() exactly, the same normalization find_anilist_id()/title_index
-- already use.
--
-- Companion fix (same issue, no schema change needed): sync_crunchyroll.py's
-- parse_items() now keys CR watch-history by (series_title, season_number) instead
-- of series_title alone, and anilist_sync_common.py's find_anilist_id() tries
-- AniList's sequel-naming conventions ("{title} 2nd Season", "{title} II", etc.)
-- before falling back to the bare title when season_number > 1.
--
-- Additive only — per CLAUDE.md's guardrail, additive migrations (new table) are
-- fine to write directly, but still get explicit confirmation before running this
-- against the live database, per this repo's general migration process.

BEGIN;

CREATE TABLE IF NOT EXISTS cr_title_overrides (
    id             SERIAL PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    series_title   TEXT NOT NULL,
    season_number  INTEGER NOT NULL DEFAULT 1,
    anilist_id     INTEGER NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, series_title, season_number)
);

COMMIT;
