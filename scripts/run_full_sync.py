#!/usr/bin/env python3
"""
Full sync pipeline orchestrator — single-user primitive.

Syncs exactly one user, specified via the USER_ID env var. The scheduled "sync every
user" loop lives in app/main.py's _scheduled_full_sync(), which invokes this script
once per eligible user; the manual "Sync Now" button does the same for just the
logged-in user. This script itself has no concept of "all users."

Reads credentials from that user's settings DB row (falling back to env vars only for
local dev/testing without a real user), then runs three independent steps:
  - crunchyroll        — sync_crunchyroll.py (fetches CR watch history directly, issue
                          #45 — no separate "fetch" sub-step or history.json anymore;
                          the vendored crunchyexporter-cli this used to shell out to has
                          been retired entirely)
  - netflix             — Netflix viewing activity → AniList progress updates
  - anilist_postgres    — AniList library → Postgres (the step that actually refreshes
                           the app's data; never skipped, only ok/error)

crunchyroll/netflix are independently guarded on that service's credentials being
configured (skipped, not failed, if absent) and — critically — a failure in one step no
longer prevents the others from running: each step's outcome is recorded on its own
`sync_log.steps` entry rather than aborting the whole pipeline (see issue #62; this used
to sys.exit(1) on the first failing step, which meant e.g. a stale Netflix cookie
silently stopped the AniList→Postgres refresh too).

Overall sync_log.status is computed from the steps: 'error' if anilist_postgres itself
failed (or AniList credentials were missing entirely, short-circuiting before any step
runs), 'partial' if anilist_postgres succeeded but crunchyroll or netflix errored, 'ok'
otherwise. A 'partial' run still exits 0 — the app's data did refresh — so the
scheduler's ok/failed Telegram summary keeps reading it as success for now; real
differentiated alerting on partial runs is issue #20, not this script.
"""

import json
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


def _start_log(sync_type: str) -> int:
    """INSERT the 'running' row for this pipeline run; returns its id. Steps get filled
    in incrementally via _update_log_steps as the run progresses, so a status/log poll
    mid-run sees live per-step progress rather than nothing until the run finishes.

    sync_type is 'full_sync' for a normal run or 'force_full_resync' when
    FORCE_FULL_RESYNC is set (issue #20) — recorded once here and never overwritten by
    _finish_log()'s later UPDATE."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sync_log (user_id, type, status, steps) VALUES (%s, %s, %s, %s) "
                "RETURNING id",
                (USER_ID, sync_type, "running", "[]"),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def _update_log_steps(log_id: int, steps: list[dict]) -> None:
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE sync_log SET steps = %s WHERE id = %s", (json.dumps(steps), log_id))
        conn.close()
    except Exception as e:
        log(f"Warning: could not update sync log steps: {e}")


def _finish_log(
    log_id: int,
    status: str,
    entries_updated: int | None,
    error_msg: str | None,
    steps: list[dict],
) -> None:
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sync_log SET status = %s, entries_updated = %s, error_msg = %s, steps = %s "
                "WHERE id = %s",
                (status, entries_updated, error_msg, json.dumps(steps), log_id),
            )
        conn.close()
    except Exception as e:
        log(f"Warning: could not finalize sync log: {e}")


def _run_step(log_id: int, steps: list[dict], service: str, fn) -> str:
    """Run one step's fn() -> (status, entries_updated, error_msg), recording a live
    'running' placeholder before it starts (so a mid-run poll sees which step is
    currently in flight) and the real result once it finishes. An unexpected exception
    inside fn() is caught here and recorded as that step's error instead of crashing the
    whole pipeline — this is what makes steps independent."""
    steps.append({"service": service, "status": "running", "entries_updated": None, "error_msg": None})
    _update_log_steps(log_id, steps)
    try:
        status, entries_updated, error_msg = fn()
    except Exception as e:
        status, entries_updated, error_msg = "error", None, f"Unexpected error: {e}"
    steps[-1] = {"service": service, "status": status, "entries_updated": entries_updated, "error_msg": error_msg}
    _update_log_steps(log_id, steps)
    if status == "error":
        log(f"ERROR: {service} — {error_msg}")
    return status


def _do_crunchyroll(
    cr_etp_rt: str, credentials_env: dict, force_full_resync: bool = False
) -> tuple[str, int | None, str | None]:
    if not cr_etp_rt:
        log("Crunchyroll — no ETP-RT configured, skipping")
        return "skipped", None, None

    log("Crunchyroll — syncing → AniList" + (" (forced full resync)" if force_full_resync else ""))
    extra_env = {**credentials_env, "CRUNCHYROLL_ETP_RT": cr_etp_rt}
    if force_full_resync:
        extra_env["FORCE_FULL_RESYNC"] = "1"
    ok = run(
        [sys.executable, str(SCRIPTS_DIR / "sync_crunchyroll.py")],
        extra_env=extra_env,
    )
    if not ok:
        return "error", None, "Crunchyroll → AniList sync failed"

    return "ok", None, None


def _do_netflix(
    netflix_cookie_header: str, netflix_profile_guid: str, credentials_env: dict
) -> tuple[str, int | None, str | None]:
    if not (netflix_cookie_header and netflix_profile_guid):
        log("Netflix — no credentials configured, skipping")
        return "skipped", None, None

    log("Netflix — syncing → AniList")
    ok = run(
        [sys.executable, str(SCRIPTS_DIR / "sync_netflix.py")],
        extra_env={
            **credentials_env,
            "NETFLIX_COOKIE_HEADER": netflix_cookie_header,
            "NETFLIX_PROFILE_GUID": netflix_profile_guid,
        },
    )
    if not ok:
        return "error", None, "Netflix → AniList sync failed"

    return "ok", None, None


def _do_anilist_postgres(credentials_env: dict) -> tuple[str, int | None, str | None]:
    log("AniList — syncing → Postgres")
    ok = run(
        [sys.executable, str(SCRIPTS_DIR / "sync_anilist.py")],
        extra_env=credentials_env,
    )
    if not ok:
        return "error", None, "AniList → Postgres sync failed"

    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM library_entries WHERE user_id = %s", (USER_ID,))
            total = cur.fetchone()[0]
        conn.close()
    except Exception:
        total = None

    return "ok", total, None


def _compute_overall_status(steps: list[dict]) -> str:
    anilist_status = next((s["status"] for s in steps if s["service"] == "anilist_postgres"), None)
    if anilist_status != "ok":
        return "error"
    if any(s["status"] == "error" for s in steps if s["service"] != "anilist_postgres"):
        return "partial"
    return "ok"


def main() -> None:
    log("Starting full sync pipeline")
    # Issue #20 — Crunchyroll-only override; see sync_crunchyroll.py's own
    # FORCE_FULL_RESYNC handling. Netflix and AniList are unaffected.
    force_full_resync = os.environ.get("FORCE_FULL_RESYNC", "").strip().lower() in ("1", "true", "yes")
    sync_type = "force_full_resync" if force_full_resync else "full_sync"
    log_id = _start_log(sync_type)
    settings = load_settings()

    # Resolve credentials: DB settings take priority over env vars (env var fallback
    # only meaningful for local dev/testing without a real settings row)
    anilist_token         = settings.get("anilist_token")         or os.environ.get("ANILIST_TOKEN", "")
    anilist_username      = settings.get("anilist_username")      or os.environ.get("ANILIST_USERNAME", "")
    cr_etp_rt             = settings.get("cr_etp_rt")             or os.environ.get("CRUNCHYROLL_ETP_RT", "")
    netflix_cookie_header  = settings.get("netflix_cookie_header")  or os.environ.get("NETFLIX_COOKIE_HEADER", "")
    netflix_profile_guid   = settings.get("netflix_profile_guid")   or os.environ.get("NETFLIX_PROFILE_GUID", "")

    if not anilist_token or not anilist_username:
        msg = "AniList credentials not configured. Set them in Settings."
        log(f"ERROR: {msg}")
        _finish_log(log_id, "error", None, msg, [])
        sys.exit(1)

    credentials_env = {
        "ANILIST_TOKEN":    anilist_token,
        "ANILIST_USERNAME": anilist_username,
        "DATABASE_URL":     DATABASE_URL,
        "USER_ID":          str(USER_ID),
    }

    steps: list[dict] = []
    _run_step(log_id, steps, "crunchyroll", lambda: _do_crunchyroll(cr_etp_rt, credentials_env, force_full_resync))
    _run_step(log_id, steps, "netflix", lambda: _do_netflix(netflix_cookie_header, netflix_profile_guid, credentials_env))
    _run_step(log_id, steps, "anilist_postgres", lambda: _do_anilist_postgres(credentials_env))

    overall_status = _compute_overall_status(steps)
    anilist_step = next(s for s in steps if s["service"] == "anilist_postgres")
    top_error_msg = anilist_step["error_msg"] if overall_status == "error" else None
    _finish_log(log_id, overall_status, anilist_step["entries_updated"], top_error_msg, steps)

    log(f"Full sync pipeline complete — overall status: {overall_status}")
    sys.exit(0 if overall_status in ("ok", "partial") else 1)


if __name__ == "__main__":
    main()
