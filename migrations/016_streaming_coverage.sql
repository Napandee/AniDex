-- Issue #182 — Streaming Coverage v1: per-user "services I own" input, scored against
-- Watching/Planning library entries by episodes-remaining, surfaced as marginal-value
-- coverage on a new /streaming page.
--
-- Additive only: one new small per-user table. No changes to any existing table.
--
-- Schema call (see issue #182's "open questions"): `service` is a free TEXT column,
-- not a foreign key into a separate services table or a DB-level CHECK enum. The
-- actual allowlist of valid values is app/main.py's existing STREAMING_SITES set
-- (same one that already filters anime.external_links down to real streaming
-- platforms elsewhere in this app) — validated in application code on write, the
-- same way library_entries.status / VALID_STATUSES is handled today (no CHECK
-- constraint on that column either). Keeping the DB schema allowlist-free means
-- STREAMING_SITES can grow (AniList adds a new externalLinks site) without a
-- migration; a stale row for a site later removed from STREAMING_SITES just stops
-- matching anything in the coverage query rather than becoming an invalid row.
--
-- No `id` surrogate key — (user_id, service) is naturally unique and is exactly what
-- every read/write here keys on, same shape as `settings` and `cr_sync_state`.
CREATE TABLE user_streaming_services (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service    TEXT NOT NULL,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, service)
);
