#!/usr/bin/env python3
"""
Full sync pipeline orchestrator — single-user primitive.

Syncs exactly one user, specified via the USER_ID env var. The scheduled "sync every
user" loop lives in app/main.py's _scheduled_full_sync(), which invokes this script
once per eligible user; the manual "Sync Now" button does the same for just the
logged-in user. This script itself has no concept of "all users."

Reads credentials from that user's settings DB row (falling back to env vars only for
local dev/testing without a real user), then runs the three sync steps in sequence:
  1. crunchyexporter-cli fetch  — pull CR watch history to history.json
  2. sync_crunchyroll.py        — CR history → AniList progress updates
  3. sync_anilist.py            — AniList library → Postgres

Exit 0 = all steps succeeded. Exit 1 = any step failed.
"""

import os
import subprocess
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])
SCRIPTS_DIR = Path(__file__).parent
# Where crunchyexporter-cli itself is vendored (fixed, global — see Dockerfile).
# Not to be confused with CRUNCHYEXPORTER_DIR below, which is per-user.
CRUNCHYEXPORTER_INSTALL_DIR = Path(os.environ.get("CRUNCHYEXPORTER_INSTALL_DIR", "/opt/crunchyexporter"))
# Per-user subdirectory for config.yaml/data — avoids one user's leftover
# history.json/config.yaml ever being read by another user's sync, even across
# separate runs. main.py itself always runs from CRUNCHYEXPORTER_INSTALL_DIR (that's
# the only place it exists); this is just cwd, so its own relative config/data reads
# land in the right per-user spot.
CRUNCHYEXPORTER_DIR = Path(os.environ.get("CRUNCHYEXPORTER_DIR", "/opt/crunchyexporter")) / str(USER_ID)
HISTORY_PATH = CRUNCHYEXPORTER_DIR / "data" / "history.json"


def log(msg: str) -> None:
    print(f"[run_full_sync] user={USER_ID} {msg}", flush=True)


def load_settings() -> dict:
    """Pull this user's settings from DB; return as dict."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT key, value FROM settings WHERE user_id = %s", (USER_ID,))
            return {row["key"]: row["value"] for row in cur.fetchall()}
    finally:
        conn.close()


def run(cmd: list[str], extra_env: dict | None = None, cwd: Path | None = None) -> bool:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(cmd, env=env, cwd=cwd)
    return result.returncode == 0


def write_log(status: str, entries_updated: int | None = None, error_msg: str | None = None) -> None:
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sync_log (user_id, type, status, entries_updated, error_msg) "
                "VALUES (%s, %s, %s, %s, %s)",
                (USER_ID, "full_sync", status, entries_updated, error_msg),
            )
        conn.close()
    except Exception as e:
        log(f"Warning: could not write sync log: {e}")


def main() -> None:
    log("Starting full sync pipeline")
    settings = load_settings()

    # Resolve credentials: DB settings take priority over env vars (env var fallback
    # only meaningful for local dev/testing without a real settings row)
    anilist_token    = settings.get("anilist_token")    or os.environ.get("ANILIST_TOKEN", "")
    anilist_username = settings.get("anilist_username") or os.environ.get("ANILIST_USERNAME", "")
    cr_etp_rt        = settings.get("cr_etp_rt")        or os.environ.get("CRUNCHYROLL_ETP_RT", "")

    if not anilist_token or not anilist_username:
        msg = "AniList credentials not configured. Set them in Settings."
        log(f"ERROR: {msg}")
        write_log("error", error_msg=msg)
        sys.exit(1)

    credentials_env = {
        "ANILIST_TOKEN":    anilist_token,
        "ANILIST_USERNAME": anilist_username,
        "DATABASE_URL":     DATABASE_URL,
        "USER_ID":          str(USER_ID),
    }

    # ── Step 1: Crunchyroll fetch ─────────────────────────────────────────────
    if cr_etp_rt:
        log("Step 1/3 — Fetching Crunchyroll watch history")
        CRUNCHYEXPORTER_DIR.mkdir(parents=True, exist_ok=True)
        (CRUNCHYEXPORTER_DIR / "data").mkdir(exist_ok=True)

        config_path = CRUNCHYEXPORTER_DIR / "config.yaml"
        config_path.write_text(f"crunchyroll:\n  etp_rt: \"{cr_etp_rt}\"\n")

        ok = run(
            [sys.executable, str(CRUNCHYEXPORTER_INSTALL_DIR / "src" / "main.py"), "fetch"],
            cwd=CRUNCHYEXPORTER_DIR,
        )
        if not ok:
            write_log("error", error_msg="Crunchyroll fetch failed")
            log("ERROR: Crunchyroll fetch failed")
            sys.exit(1)

        # ── Step 2: CR → AniList sync ─────────────────────────────────────────
        log("Step 2/3 — Syncing Crunchyroll → AniList")
        ok = run(
            [sys.executable, str(SCRIPTS_DIR / "sync_crunchyroll.py")],
            extra_env={**credentials_env, "HISTORY_PATH": str(HISTORY_PATH)},
        )
        if not ok:
            write_log("error", error_msg="Crunchyroll → AniList sync failed")
            log("ERROR: Crunchyroll → AniList sync failed")
            sys.exit(1)
    else:
        log("Step 1-2/3 — No Crunchyroll ETP-RT configured, skipping CR sync")

    # ── Step 3: AniList → Postgres sync ──────────────────────────────────────
    log("Step 3/3 — Syncing AniList → Postgres")
    ok = run(
        [sys.executable, str(SCRIPTS_DIR / "sync_anilist.py")],
        extra_env=credentials_env,
    )
    if not ok:
        write_log("error", error_msg="AniList → Postgres sync failed")
        log("ERROR: AniList → Postgres sync failed")
        sys.exit(1)

    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM library_entries WHERE user_id = %s", (USER_ID,))
            total = cur.fetchone()[0]
        conn.close()
    except Exception:
        total = None

    write_log("ok", entries_updated=total)
    log("Full sync pipeline complete")


if __name__ == "__main__":
    main()
