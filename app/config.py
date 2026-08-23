"""Per-user settings backed by the settings table (PK: user_id, key)."""

import os

from cryptography.fernet import Fernet, InvalidToken

from app import db

DEFAULTS = {
    "timezone": "Europe/London",
    "language": "en",
    "theme": "system",
}

# Issue #310 — these settings keys hold live, currently-usable credentials for
# external services (AniList OAuth token, Crunchyroll/Netflix session cookies).
# A DB leak of any of these is a session-hijack on the real external account, not
# just AniDex access, so they're encrypted at rest with a Fernet key from
# SETTINGS_ENCRYPTION_KEY (see below). Confirmed against every
# config.set_value()/config.get() call site in app/main.py — this is the complete
# and exact list; everything else written through this module (timezone, language,
# theme, notification toggles, hidden_tags, etc.) stays plaintext on purpose: no
# real security benefit to encrypting those, and it would make them unreadable to
# a human glancing at the settings table for debugging.
#
# Issue #319 — telegram_bot_token/discord_webhook_url/ntfy_auth_token added: found
# during #310's own call-site grep, deliberately split into a follow-up rather than
# folded into that PR (per this repo's issue-first convention for mid-session
# finds). Same DB-leak exposure shape as #310's keys, just a smaller blast radius
# (post into a Telegram chat / Discord channel / ntfy topic, not hijack a real
# streaming account) — see app/notify.py's config.get() call sites for where these
# get read back for an actual send.
ENCRYPTED_KEYS = {
    "anilist_token",
    "cr_etp_rt",
    "netflix_cookie_header",
    "netflix_profile_guid",
    "telegram_bot_token",
    "discord_webhook_url",
    "ntfy_auth_token",
}

_SETTINGS_ENCRYPTION_KEY = os.environ.get("SETTINGS_ENCRYPTION_KEY")
if not _SETTINGS_ENCRYPTION_KEY:
    raise RuntimeError(
        "SETTINGS_ENCRYPTION_KEY is not set. This app encrypts AniList/Crunchyroll/"
        "Netflix credentials and the TOTP 2FA secret at rest (issue #310) — unlike "
        "SESSION_SECRET_KEY, there is no safe random fallback here: a key generated "
        "fresh on every process start would make already-encrypted data permanently "
        "unreadable the moment the process restarts. Generate one with:\n"
        "  python3 -c \"from cryptography.fernet import Fernet; "
        "print(Fernet.generate_key().decode())\"\n"
        "and set it as the SETTINGS_ENCRYPTION_KEY env var (see .env.example)."
    )

try:
    _fernet = Fernet(_SETTINGS_ENCRYPTION_KEY)
except Exception as e:
    raise RuntimeError(
        f"SETTINGS_ENCRYPTION_KEY is set but is not a valid Fernet key: {e}. "
        "Generate a real one with: python3 -c \"from cryptography.fernet import "
        "Fernet; print(Fernet.generate_key().decode())\""
    ) from e


def is_encrypted(value) -> bool:
    """True if `value` decrypts cleanly with the current Fernet key — i.e. it's
    already ciphertext produced by encrypt_secret(), not plaintext. Used by
    decrypt_secret() itself and by scripts/migrate_encrypt_credentials.py's
    idempotency guard (issue #310), so re-running that migration against
    already-encrypted data is always a safe no-op rather than double-encrypting."""
    if not value:
        return False
    try:
        _fernet.decrypt(value.encode())
        return True
    except (InvalidToken, ValueError, TypeError):
        return False


def encrypt_secret(value: str) -> str:
    """Encrypts a plaintext string for storage. Empty/None passes through
    unchanged — callers never persist an empty-string sentinel for these keys
    today (the Settings credential-save routes only call this when the submitted
    field is non-empty), but this keeps the function safe to call defensively."""
    if not value:
        return value
    return _fernet.encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    """Decrypts a value written by encrypt_secret(). Passes through unchanged if
    empty or not a recognizable Fernet token (via is_encrypted()) — this means a
    value that hasn't been through the migration script yet (still plaintext) is
    still read correctly, so deploying this code and running the data migration
    can happen as two separate steps without breaking anything in between."""
    if not value or not is_encrypted(value):
        return value
    return _fernet.decrypt(value.encode()).decode()


def get_all(user_id: int) -> dict:
    rows = db.fetchall("SELECT key, value FROM settings WHERE user_id = %s", (user_id,))
    result = dict(DEFAULTS)
    result.update({r["key"]: r["value"] for r in rows})
    for key in ENCRYPTED_KEYS:
        if key in result:
            result[key] = decrypt_secret(result[key])
    return result


def get(user_id: int, key: str) -> str:
    row = db.fetchone(
        "SELECT value FROM settings WHERE user_id = %s AND key = %s", (user_id, key)
    )
    value = row["value"] if row else DEFAULTS.get(key, "")
    if key in ENCRYPTED_KEYS:
        return decrypt_secret(value)
    return value


def set_value(user_id: int, key: str, value: str) -> None:
    stored_value = encrypt_secret(value) if key in ENCRYPTED_KEYS else value
    db.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
        (user_id, key, stored_value),
    )
