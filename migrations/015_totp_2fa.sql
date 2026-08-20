-- Issue #83 — TOTP-based two-factor authentication for local accounts.
--
-- Additive only: new columns on users, one new table for hashed one-time
-- recovery codes.
--
-- totp_secret is only ever populated once setup is CONFIRMED — a valid 6-digit
-- code from the authenticator app was verified (see app/main.py's
-- POST /settings/2fa/setup). An in-progress setup's pending secret is held
-- server-side, in-process, keyed by user_id (app/main.py's _totp_setup_state) —
-- deliberately never written here and never round-tripped through the client-
-- visible session cookie (that cookie is itsdangerous-*signed*, not encrypted, so
-- it's the wrong place for a real secret) — so an abandoned setup never leaves an
-- unconfirmed secret anywhere a client or this table can see it.
--
-- totp_recovery_codes holds bcrypt hashes only, same standard as users.password_hash
-- — the plaintext codes are shown to the user exactly once (at enable time) and are
-- never persisted anywhere. used_at marks a code consumed (single-use); a NULL
-- used_at row is a still-valid recovery code.
--
-- totp_failed_attempts/totp_locked_until are a SEPARATE lockout counter from the
-- existing failed_login_attempts/locked_until (which stays scoped to password
-- guesses only, including the /settings/2fa/disable re-auth check) — kept apart so
-- an unrelated password reset (which clears failed_login_attempts/locked_until)
-- can never accidentally re-arm an in-progress TOTP-code brute-force lockout.

ALTER TABLE users ADD COLUMN totp_secret TEXT;
ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN totp_enabled_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN totp_failed_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN totp_locked_until TIMESTAMPTZ;

CREATE TABLE totp_recovery_codes (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash   TEXT NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_totp_recovery_codes_user ON totp_recovery_codes(user_id) WHERE used_at IS NULL;
