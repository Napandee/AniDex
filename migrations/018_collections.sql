-- Issue #200 — Collections: named saved filter combinations over the library view's
-- existing tag/status/score/format/season/rewatch/sort controls.
--
-- Renumbered 017 -> 018 (same pattern as the earlier 016->017 renumber this
-- session): #199 (issue #185, recommend-outcome tracking) merged
-- 017_recommendation_first_shown_at.sql to main first, while this branch was
-- still cut from an older main — collided on 017 once rebased/merged forward.
--
-- Additive only: one new small per-user table. No changes to any existing table,
-- and no new per-anime relationship — `filters` is a whitelisted snapshot of the
-- library view's own client-side filter/sort state (see app/main.py's
-- COLLECTION_FILTER_KEYS and _sanitize_collection_filters), never a list of anime
-- ids. Applying a collection just re-drives the existing filter controls; nothing
-- reads this table to decide which anime "belong" to it.
--
-- UNIQUE (user_id, name) lets rename/create surface a clean 409 instead of two
-- collections silently sharing a name in the same account's dropdown.
CREATE TABLE collections (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    filters     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

CREATE INDEX idx_collections_user ON collections (user_id);
