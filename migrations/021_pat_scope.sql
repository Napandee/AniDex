-- Migration 021: read/write scope for personal access tokens (issue #208).
--
-- #207 shipped PATs with no scope distinction, deliberately — MCP had no write
-- tools yet, so a read-vs-write split was moot (see app/pat.py's #207-era note
-- and issue #208's own text). #208 adds write-capable MCP tools
-- (update_personal_notes, bulk_apply_tags, set_rating, set_status,
-- set_progress), so every PAT now needs an explicit scope: 'read' can only call
-- the four read-only tools from #207; 'read_write' can additionally call the
-- five write tools. app/mcp_server.py's _require_write_scope() is the actual
-- enforcement point — this column is what it checks.
--
-- Every existing token backfills to 'read' (the safe default) — nobody's
-- already-issued token silently gains write access from this migration. A
-- write-scoped token has to be freshly issued after this ships, with the user
-- explicitly choosing "read + write" in the Settings token-creation form.
--
-- Additive only (new column with a safe default, no drops, no existing data
-- touched) — per CLAUDE.md's guardrail this is fine to write directly, but
-- still get explicit confirmation before running it against the live database.

BEGIN;

ALTER TABLE personal_access_tokens
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'read';

ALTER TABLE personal_access_tokens
    DROP CONSTRAINT IF EXISTS personal_access_tokens_scope_check;

ALTER TABLE personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_scope_check CHECK (scope IN ('read', 'read_write'));

COMMIT;
