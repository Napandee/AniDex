-- Migration 022: add invites.expires_at + invites.revoked_at — issue #231.
--
-- The `invites` table had no expiry concept at all: a pending row created
-- with ON CONFLICT (email) DO NOTHING lived forever, with no way to see its
-- state (pending/expired/accepted) or to resend/revoke it short of direct
-- database surgery — the exact "invited limbo" pain point called out in
-- #231's Context section (observed in Vaultwarden's own invite system).
--
-- expires_at: NOT NULL, defaults to 7 days out at insert time
-- (INVITE_EXPIRY_DAYS in app/main.py). Enforced at signup in
-- _resolve_or_create_user (app/main.py) — a matching invite row must have
-- accepted_at IS NULL, revoked_at IS NULL, AND expires_at > now(), so an
-- expired or revoked row silently stops granting signup access without
-- needing to be deleted.
--
-- revoked_at: nullable, set by the new POST /admin/invites/{id}/revoke
-- endpoint to invalidate an outstanding invite before it's used. Cleared
-- again by POST /admin/invites/{id}/resend, which also pushes expires_at
-- back out by another INVITE_EXPIRY_DAYS — the "reissue a fresh invite"
-- action from #231's scope. Both endpoints guard WHERE accepted_at IS NULL
-- in SQL (RETURNING id/email, checked for a row in app/main.py) since
-- neither action makes sense once an invite has already been used to create
-- an account.
--
-- Backfilled every pre-existing row's expires_at to created_at + 7 days
-- rather than leaving it NULL — NULL would either need a "never expires"
-- special case (defeating the point of this migration for every invite that
-- predates it) or an awkward separate migration-time cutoff. An already-old
-- pending invite backfilled this way simply becomes immediately expired,
-- which is the correct/intended outcome: it visibly moves to "expired" in
-- Admin -> Invites with a Resend action available, rather than silently
-- staying valid forever.
--
-- Also rebuilds idx_invites_email to match the new three-clause lookup
-- predicate in _resolve_or_create_user (accepted_at IS NULL AND revoked_at
-- IS NULL) instead of just accepted_at IS NULL, so the partial index still
-- covers the query it's meant to speed up.
--
-- Purely additive (two new nullable/backfilled columns, index rebuild — no
-- existing column altered or dropped, no data removed) — per CLAUDE.md's
-- guardrail, additive migrations are fine to write directly, but get
-- explicit confirmation before running this against the live database.

BEGIN;

ALTER TABLE invites ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE invites ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;

UPDATE invites SET expires_at = created_at + INTERVAL '7 days' WHERE expires_at IS NULL;

ALTER TABLE invites ALTER COLUMN expires_at SET DEFAULT (now() + INTERVAL '7 days');
ALTER TABLE invites ALTER COLUMN expires_at SET NOT NULL;

DROP INDEX IF EXISTS idx_invites_email;
CREATE INDEX idx_invites_email ON invites(email) WHERE accepted_at IS NULL AND revoked_at IS NULL;

COMMIT;
