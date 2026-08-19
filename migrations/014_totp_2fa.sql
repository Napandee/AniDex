-- Issue #83 — TOTP-based two-factor authentication for local accounts.
--
-- Additive only: two new columns on users, one new table for hashed one-time
-- recovery codes.
--
-- totp_secret is only ever populated once setup is CONFIRMED — a valid 6-digit
-- code from the authenticator app was verified (see app/main.py's
-- POST /settings/2fa/setup). An in-progress setup's pending secret lives only in
-- the signed session cookie (same SESSION_SECRET_KEY-backed session already used
-- for login state), never written here, so an abandoned setup never leaves an
-- unconfirmed secret sitting in the database.
--
-- totp_recovery_codes holds bcrypt hashes only, same standard as users.password_hash
-- — the plaintext codes are shown to the user exactly once (at enable time) and are
-- never persisted anywhere. used_at marks a code consumed (single-use); a NULL
-- used_at row is a still-valid recovery code.

ALTER TABLE users ADD COLUMN totp_secret TEXT;
ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN totp_enabled_at TIMESTAMPTZ;

CREATE TABLE totp_recovery_codes (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash   TEXT NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_totp_recovery_codes_user ON totp_recovery_codes(user_id) WHERE used_at IS NULL;
