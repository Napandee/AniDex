"""
Coverage for issue #310 — encrypting AniList/Crunchyroll/Netflix credentials and
the TOTP 2FA secret at rest.

Three layers:
  - app.config's encrypt_secret()/decrypt_secret()/is_encrypted() are pure
    functions, covered directly with no DB involved.
  - config.get()/config.set_value()/config.get_all() round-trip through a real
    Postgres `settings` table (same "skip entirely if Postgres isn't reachable"
    pattern as tests/test_admin_instance_health.py etc.), proving the raw DB
    value is genuinely ciphertext for the sensitive keys and genuinely plaintext
    for everything else.
  - scripts/migrate_encrypt_credentials.py's migrate_settings()/
    migrate_totp_secrets() are exercised against a real Postgres seeded with
    realistic plaintext values (a fake ETP-RT-shaped cookie, a fake base32 TOTP
    secret), proving the migration's idempotency guard: running it twice never
    double-encrypts.
  - A missing SETTINGS_ENCRYPTION_KEY env var fails app.config's import loudly
    (not a silent plaintext fallback, not a randomly-generated key) — this is
    checked in a fresh subprocess, since app.config only evaluates the env var
    once at import time and every other test in this suite depends on it having
    already imported successfully.

Needs a reachable Postgres via DATABASE_URL for the DB-backed tests (the same
throwaway-Postgres pattern .github/workflows/pr-validate.yml provisions) — those
are skipped if one isn't available, so `pytest tests/` still collects and passes
on a machine with no Postgres running. The pure-function and subprocess tests
run unconditionally.
"""

import os
import subprocess
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import config

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()
REPO_ROOT = Path(__file__).resolve().parent.parent


def _try_connect():
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=2)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception:
        return None


@pytest.fixture(scope="module")
def pg_conn():
    conn = _try_connect()
    if conn is None:
        pytest.skip(
            f"No reachable Postgres at {DATABASE_URL} — this suite needs a real "
            "throwaway instance (same one .github/workflows/pr-validate.yml provisions)."
        )
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(SCHEMA_SQL)
    yield conn
    conn.close()


_next_user_id = [5000]


def _make_user(pg_conn):
    _next_user_id[0] += 1
    uid = _next_user_id[0]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, is_admin) "
            "VALUES (%s, 'local', %s, %s, false)",
            (uid, f"user{uid}", f"user{uid}@example.com"),
        )
    return uid


# ── Pure-function coverage ───────────────────────────────────────────────────


def test_encrypt_decrypt_round_trip():
    plaintext = "fake-etp-rt-cookie-value-abc123"
    ciphertext = config.encrypt_secret(plaintext)
    assert ciphertext != plaintext
    assert config.decrypt_secret(ciphertext) == plaintext


def test_encrypt_produces_recognizable_fernet_token():
    ciphertext = config.encrypt_secret("some-real-looking-session-cookie")
    assert config.is_encrypted(ciphertext)


def test_plaintext_is_not_recognized_as_encrypted():
    assert not config.is_encrypted("plain-old-cookie-value")
    assert not config.is_encrypted("JBSWY3DPEHPK3PXP")  # base32 TOTP-secret-shaped


def test_decrypt_passes_through_plaintext_unchanged():
    # A value that hasn't been through the migration script yet — decrypt_secret()
    # must not raise, and must return it unchanged, so the app keeps working
    # between "code deployed" and "migration run" (two deliberately separate steps).
    assert config.decrypt_secret("still-plaintext-cookie") == "still-plaintext-cookie"


def test_empty_and_none_pass_through():
    assert config.encrypt_secret("") == ""
    assert config.encrypt_secret(None) is None
    assert config.decrypt_secret("") == ""
    assert config.decrypt_secret(None) is None


# ── config.get()/set_value()/get_all() round-trip against real Postgres ─────


def test_sensitive_key_stored_as_ciphertext_in_raw_db(pg_conn):
    uid = _make_user(pg_conn)
    plaintext = "AL_ANILIST_FAKE_TOKEN_xyz"
    config.set_value(uid, "anilist_token", plaintext)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE user_id = %s AND key = %s", (uid, "anilist_token"))
        raw_value = cur.fetchone()[0]

    assert raw_value != plaintext
    assert config.is_encrypted(raw_value)
    assert config.get(uid, "anilist_token") == plaintext


@pytest.mark.parametrize(
    "key,value",
    [
        ("anilist_token", "AL_TOKEN_abc"),
        ("cr_etp_rt", "fake_etp_rt_cookie_value_1234567890"),
        ("netflix_cookie_header", "NetflixId=fake; SecureNetflixId=fake2; nfvdid=fake3"),
        ("netflix_profile_guid", "12345678-abcd-4321-9999-fedcba987654"),
    ],
)
def test_every_encrypted_key_round_trips(pg_conn, key, value):
    uid = _make_user(pg_conn)
    config.set_value(uid, key, value)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE user_id = %s AND key = %s", (uid, key))
        raw_value = cur.fetchone()[0]

    assert raw_value != value, f"{key} was stored as plaintext"
    assert config.get(uid, key) == value
    assert config.get_all(uid)[key] == value


def test_non_sensitive_key_stays_plaintext(pg_conn):
    uid = _make_user(pg_conn)
    config.set_value(uid, "timezone", "America/New_York")

    with pg_conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE user_id = %s AND key = %s", (uid, "timezone"))
        raw_value = cur.fetchone()[0]

    assert raw_value == "America/New_York"
    assert config.get(uid, "timezone") == "America/New_York"


def test_encrypted_keys_allowlist_matches_confirmed_call_sites():
    # Locks in the confirmed-against-app/main.py allowlist from the issue — guards
    # against silent drift if a future call site adds/renames a sensitive key
    # without updating ENCRYPTED_KEYS.
    assert config.ENCRYPTED_KEYS == {
        "anilist_token",
        "cr_etp_rt",
        "netflix_cookie_header",
        "netflix_profile_guid",
    }


# ── TOTP secret read/write sites (app/main.py) ───────────────────────────────


def test_totp_secret_stored_encrypted_and_verifiable(pg_conn):
    import pyotp

    uid = _make_user(pg_conn)
    secret = pyotp.random_base32()
    encrypted = config.encrypt_secret(secret)

    with pg_conn.cursor() as cur:
        cur.execute("UPDATE users SET totp_secret = %s WHERE id = %s", (encrypted, uid))

    with pg_conn.cursor() as cur:
        cur.execute("SELECT totp_secret FROM users WHERE id = %s", (uid,))
        raw_value = cur.fetchone()[0]

    assert raw_value != secret
    assert config.is_encrypted(raw_value)

    # Mirrors the real login-verification call site (app/main.py's
    # auth_login_2fa_submit): decrypt then verify a real generated code.
    decrypted = config.decrypt_secret(raw_value)
    code = pyotp.TOTP(decrypted).now()
    assert pyotp.TOTP(decrypted).verify(code, valid_window=1)


# ── Migration script (scripts/migrate_encrypt_credentials.py) ───────────────


def test_migration_encrypts_existing_plaintext_and_is_idempotent(pg_conn):
    import migrate_encrypt_credentials as mig

    uid = _make_user(pg_conn)
    # Insert plaintext rows directly, bypassing config.set_value() — simulates
    # data written by the pre-#310 code before this migration ever runs.
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (%s, 'anilist_token', %s)",
            (uid, "plain_al_token_value"),
        )
        cur.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (%s, 'cr_etp_rt', %s)",
            (uid, "plain_etp_rt_cookie_value"),
        )
        # A non-sensitive key must never be touched by the migration.
        cur.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (%s, 'timezone', %s)",
            (uid, "Europe/London"),
        )
        cur.execute(
            "UPDATE users SET totp_secret = %s WHERE id = %s",
            ("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP", uid),
        )

    conn = psycopg2.connect(DATABASE_URL)
    try:
        first_settings = mig.migrate_settings(conn, dry_run=False)
        first_totp = mig.migrate_totp_secrets(conn, dry_run=False)
    finally:
        conn.close()

    assert first_settings["anilist_token"]["migrated"] == 1
    assert first_settings["cr_etp_rt"]["migrated"] == 1
    assert first_totp["migrated"] == 1

    with pg_conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE user_id = %s AND key = 'anilist_token'", (uid,))
        al_ciphertext_1 = cur.fetchone()[0]
        cur.execute("SELECT value FROM settings WHERE user_id = %s AND key = 'timezone'", (uid,))
        tz_value = cur.fetchone()[0]
        cur.execute("SELECT totp_secret FROM users WHERE id = %s", (uid,))
        totp_ciphertext_1 = cur.fetchone()[0]

    assert config.is_encrypted(al_ciphertext_1)
    assert config.decrypt_secret(al_ciphertext_1) == "plain_al_token_value"
    assert tz_value == "Europe/London"  # untouched, still plaintext
    assert config.is_encrypted(totp_ciphertext_1)

    # Run again — the idempotency guard (is_encrypted() checked before encrypting)
    # must report these rows as already-encrypted and leave the ciphertext
    # byte-for-byte unchanged, not double-encrypt them.
    conn = psycopg2.connect(DATABASE_URL)
    try:
        second_settings = mig.migrate_settings(conn, dry_run=False)
        second_totp = mig.migrate_totp_secrets(conn, dry_run=False)
    finally:
        conn.close()

    # Global counts (this table/module accumulates other tests' rows in the same
    # pg_conn) — assert the "nothing new got migrated" invariant with >= 1 rather
    # than an exact count, since other tests in this module also write encrypted
    # anilist_token/totp_secret rows that legitimately show up as already_encrypted
    # here too.
    assert second_settings["anilist_token"]["migrated"] == 0
    assert second_settings["anilist_token"]["already_encrypted"] >= 1
    assert second_settings["cr_etp_rt"]["migrated"] == 0
    assert second_settings["cr_etp_rt"]["already_encrypted"] >= 1
    assert second_totp["migrated"] == 0
    assert second_totp["already_encrypted"] >= 1

    with pg_conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE user_id = %s AND key = 'anilist_token'", (uid,))
        al_ciphertext_2 = cur.fetchone()[0]
        cur.execute("SELECT totp_secret FROM users WHERE id = %s", (uid,))
        totp_ciphertext_2 = cur.fetchone()[0]

    assert al_ciphertext_2 == al_ciphertext_1, "re-running the migration must not re-encrypt an already-encrypted value"
    assert totp_ciphertext_2 == totp_ciphertext_1
    # And the value is still correctly readable after two passes.
    assert config.decrypt_secret(al_ciphertext_2) == "plain_al_token_value"


def test_migration_dry_run_writes_nothing(pg_conn):
    import migrate_encrypt_credentials as mig

    uid = _make_user(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (%s, 'netflix_cookie_header', %s)",
            (uid, "plain_netflix_cookie_value"),
        )

    conn = psycopg2.connect(DATABASE_URL)
    try:
        counts = mig.migrate_settings(conn, dry_run=True)
    finally:
        conn.close()

    assert counts["netflix_cookie_header"]["migrated"] == 1

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM settings WHERE user_id = %s AND key = 'netflix_cookie_header'", (uid,)
        )
        raw_value = cur.fetchone()[0]

    assert raw_value == "plain_netflix_cookie_value"  # dry-run must not have written anything


# ── Missing/invalid SETTINGS_ENCRYPTION_KEY fails loudly at import time ─────


def test_missing_encryption_key_fails_import_loudly():
    env = {k: v for k, v in os.environ.items() if k != "SETTINGS_ENCRYPTION_KEY"}
    env.setdefault("DATABASE_URL", DATABASE_URL)
    result = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "SETTINGS_ENCRYPTION_KEY" in result.stderr
    assert "RuntimeError" in result.stderr


def test_invalid_encryption_key_fails_import_loudly():
    env = dict(os.environ)
    env["SETTINGS_ENCRYPTION_KEY"] = "not-a-real-fernet-key"
    env.setdefault("DATABASE_URL", DATABASE_URL)
    result = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "SETTINGS_ENCRYPTION_KEY" in result.stderr
    assert "RuntimeError" in result.stderr
