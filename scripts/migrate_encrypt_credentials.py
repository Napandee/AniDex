#!/usr/bin/env python3
"""
One-off data migration for issue #310 (extended by #319) — encrypts already-live
plaintext values for every key in app.config.ENCRYPTED_KEYS (anilist_token,
cr_etp_rt, netflix_cookie_header, netflix_profile_guid, telegram_bot_token,
discord_webhook_url, ntfy_auth_token) and the users.totp_secret column, in
place, using the Fernet key from SETTINGS_ENCRYPTION_KEY. This loop iterates
config.ENCRYPTED_KEYS itself rather than a hardcoded key list, so #319's three
additional keys needed no changes here — only the allowlist in app/config.py.
Code-level encrypt/decrypt on the read/write paths (app/config.py, and the TOTP
setup/verify call sites in app/main.py) ships alongside this script in the same
PR — this script only exists to bring values that were already written to the
database *before* that code shipped up to the same encrypted-at-rest state.

This is a genuine transformation of live, currently-working credentials (a real
AniList OAuth token, real Crunchyroll/Netflix session cookies, a real TOTP
secret), not a routine additive migration — per this repo's CLAUDE.md migration
guardrails, back up the database first and get EXPLICIT confirmation before
running this against a real production database. This script must not be run
by an agent against anything other than a local throwaway Postgres instance
without that explicit human confirmation — see the PR this shipped in for the
"NOT run against prod" statement.

Idempotency: for every candidate row, app.config.is_encrypted() tries to
decrypt the stored value with the current Fernet key first. A value that
decrypts cleanly is already ciphertext and is left untouched; only a value
that fails (i.e. is still plaintext) gets encrypted and written back. Running
this script twice in a row is therefore a safe no-op the second time — nothing
gets double-encrypted.

Usage:
    python scripts/migrate_encrypt_credentials.py [--dry-run]

--dry-run reports what would change (counts only, never prints the actual
plaintext/ciphertext values) without writing anything.

Requires .env (or env vars) with: DATABASE_URL, SETTINGS_ENCRYPTION_KEY
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import psycopg2
import psycopg2.extras

from app import config, db


def migrate_settings(conn, dry_run: bool) -> dict:
    counts = {key: {"migrated": 0, "already_encrypted": 0} for key in config.ENCRYPTED_KEYS}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT user_id, key, value FROM settings WHERE key = ANY(%s)",
            (list(config.ENCRYPTED_KEYS),),
        )
        rows = cur.fetchall()

    for row in rows:
        key = row["key"]
        value = row["value"]
        if config.is_encrypted(value):
            counts[key]["already_encrypted"] += 1
            continue
        counts[key]["migrated"] += 1
        if not dry_run:
            encrypted = config.encrypt_secret(value)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE settings SET value = %s WHERE user_id = %s AND key = %s",
                    (encrypted, row["user_id"], key),
                )
    if not dry_run:
        conn.commit()
    return counts


def migrate_totp_secrets(conn, dry_run: bool) -> dict:
    counts = {"migrated": 0, "already_encrypted": 0}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, totp_secret FROM users WHERE totp_secret IS NOT NULL")
        rows = cur.fetchall()

    for row in rows:
        value = row["totp_secret"]
        if config.is_encrypted(value):
            counts["already_encrypted"] += 1
            continue
        counts["migrated"] += 1
        if not dry_run:
            encrypted = config.encrypt_secret(value)
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET totp_secret = %s WHERE id = %s", (encrypted, row["id"]))
    if not dry_run:
        conn.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change (counts only) without writing anything.",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("DRY RUN — no changes will be written.\n")

    conn = psycopg2.connect(db.DATABASE_URL)
    try:
        settings_counts = migrate_settings(conn, args.dry_run)
        totp_counts = migrate_totp_secrets(conn, args.dry_run)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("=== settings (issue #310 ENCRYPTED_KEYS) ===")
    total_migrated = 0
    for key, c in settings_counts.items():
        print(f"  {key}: migrated={c['migrated']}  already_encrypted={c['already_encrypted']}")
        total_migrated += c["migrated"]

    print("\n=== users.totp_secret ===")
    print(f"  migrated={totp_counts['migrated']}  already_encrypted={totp_counts['already_encrypted']}")
    total_migrated += totp_counts["migrated"]

    print(f"\nDone. {total_migrated} value(s) {'would be ' if args.dry_run else ''}encrypted in this pass.")
    if args.dry_run:
        print("Re-run without --dry-run to apply.")


if __name__ == "__main__":
    main()
