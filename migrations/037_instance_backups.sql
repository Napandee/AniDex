-- Migration 037: add instance_backups (issue #372 — scheduled automatic backups).
--
-- Stores each scheduled backup's zip content directly in Postgres (bytea) rather than
-- on the app container's own filesystem: the app container has no persistent volume
-- mounted for arbitrary files today (only Postgres itself does, via
-- compose/anidex-postgres.yml's postgres-data mount) — reusing the database's existing,
-- already-backed-up volume avoids adding a new Docker volume / deploy-pipeline change,
-- which CLAUDE.md's guardrail says needs explicit confirmation. Off-instance delivery
-- (S3, etc.) is explicitly out of scope for this issue.
--
-- Purely additive (new table only) — per CLAUDE.md's guardrail, fine to run directly,
-- but back up first anyway per this repo's standing practice for any migration
-- touching a live database. Remember to also bump app/main.py's LATEST_MIGRATION and
-- run scripts/mark_migration_applied.sh 37 (issue #380) when this is actually applied.

BEGIN;

CREATE TABLE instance_backups (
    id            SERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    size_bytes    INTEGER NOT NULL,
    user_count    INTEGER NOT NULL,
    content       BYTEA NOT NULL
);

CREATE INDEX idx_instance_backups_created_at ON instance_backups (created_at DESC);

COMMIT;
