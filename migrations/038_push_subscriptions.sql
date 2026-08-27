-- Migration 038: add push_subscriptions (issue #377 — Web Push notifications).
--
-- One row per browser/device a user has subscribed from (a user can have several —
-- phone, desktop, etc. — each with its own subscription). `endpoint`/`p256dh`/`auth`
-- are exactly the fields the browser's PushManager.subscribe() call returns; sent
-- back to the push service that endpoint belongs to on every notify(), no other
-- credential needed (see app/vapid.py's module docstring for why). Unique on
-- (user_id, endpoint) so re-subscribing the same browser (e.g. after a permission
-- re-prompt) upserts rather than accumulating duplicate rows.
--
-- Purely additive (new table only) — per CLAUDE.md's guardrail, fine to run directly,
-- but back up first anyway per this repo's standing practice for any migration
-- touching a live database. Remember to also bump app/main.py's LATEST_MIGRATION and
-- run scripts/mark_migration_applied.sh 38 (issue #380) when this is actually applied.

BEGIN;

CREATE TABLE push_subscriptions (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint    TEXT NOT NULL,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, endpoint)
);

CREATE INDEX idx_push_subscriptions_user ON push_subscriptions (user_id);

COMMIT;
