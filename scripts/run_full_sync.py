#!/usr/bin/env python3
"""
Full sync pipeline orchestrator — single-user primitive.

Syncs exactly one user, specified via the USER_ID env var. The scheduled "sync every
user" loop lives in app/main.py's _scheduled_full_sync(), which invokes this script
once per eligible user; the manual "Sync Now" button does the same for just the
logged-in user. This script itself has no concept of "all users."

Reads credentials from that user's settings DB row (falling back to env vars only for
local dev/testing without a real user), then runs three independent steps, in this
order:
  - anilist_postgres    — AniList library → Postgres (the step that actually refreshes
                           the app's data; never skipped, only ok/error). Runs first
                           (issue #99) so crunchyroll/netflix's title matching can read
                           a fresh local library_entries mirror instead of each making
                           its own live AniList list-fetch call.
  - crunchyroll         — sync_crunchyroll.py (fetches CR watch history directly, issue
                          #45 — no separate "fetch" sub-step or history.json anymore;
                          the vendored crunchyexporter-cli this used to shell out to has
                          been retired entirely)
  - netflix             — Netflix viewing activity → AniList progress updates

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402 — decrypt_secret()/ENCRYPTED_KEYS for load_settings()

DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])
SCRIPTS_DIR = Path(__file__).parent

# Issue #91 — per-step timeouts, so one slow step can no longer starve the others (a
# single blunt external timeout used to wrap this whole script as one all-or-nothing
# unit — see app/main.py's _run_sync_task). A from-scratch or forced-full walk is
# bounded only by MAX_PAGES in sync_crunchyroll.py/sync_netflix.py (effectively
# unbounded), so these get a generous budget; anilist_postgres is a bounded Postgres
# upsert loop over the AniList list, so gets a tighter one. Not conditioned on
# FORCE_FULL_RESYNC — an ordinary first-ever sync (no stored watermark yet) faces the
# same unbounded-walk risk as a forced one, so the real risk factor is "no watermark,"
# not the flag itself.
PROVIDER_STEP_TIMEOUT = 480  # seconds — crunchyroll / netflix
ANILIST_STEP_TIMEOUT = 240  # seconds — anilist_postgres


def log(msg: str) -> None:
    print(f"[run_full_sync] user={USER_ID} {msg}", flush=True)


def load_settings() -> dict:
    """Pull this user's settings from DB; return as dict.

    Issue #310 — anilist_token/cr_etp_rt/netflix_cookie_header/netflix_profile_guid
    are stored Fernet-encrypted (app.config.ENCRYPTED_KEYS). This script talks to
    Postgres directly rather than through app.config.get_all() (it's a standalone
    single-user process, not running inside the app), so it must decrypt those keys
    itself here — otherwise every credential handed to the crunchyroll/netflix/anilist
    subprocess steps below would be raw ciphertext instead of a real usable
    token/cookie, breaking every sync silently once issue #310's migration runs."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT key, value FROM settings WHERE user_id = %s", (USER_ID,))
            result = {row["key"]: row["value"] for row in cur.fetchall()}
    finally:
        conn.close()
    for key in config.ENCRYPTED_KEYS:
        if key in result:
            result[key] = config.decrypt_secret(result[key])
    return result


def run(cmd: list[str], extra_env: dict | None = None, cwd: Path | None = None, timeout: int | None = None) -> dict:
    """Runs cmd with output captured (rather than inherited) so this can parse a
    trailing `SYNC_RESULT: {...}` JSON line the child scripts emit (issue #46) — the
    only channel this orchestrator has for learning a step's real entry counts back
    from the subprocess, since the exit code alone only tells us pass/fail. The full
    captured output is still echoed to this process's own stdout afterward, so
    container logs keep the complete per-step output — just no longer streamed
    live line-by-line while the step is running.

    Returns {"ok": bool, "entries_updated": int|None, "entries_fetched": int|None,
    "full_pull": bool|None} — the last three keys come straight from the child's
    SYNC_RESULT line and are all None if it didn't emit one (e.g. it failed before
    reaching that point).

    subprocess.TimeoutExpired is deliberately not caught here — it propagates up to
    _run_step's existing generic exception handler (issue #91), which already turns
    any unexpected exception in a step into that step's own error status without
    aborting the rest of the pipeline. str(TimeoutExpired) reads as e.g. "Command
    [...] timed out after 480 seconds", which is already a clear per-step error."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        cmd, env=env, cwd=cwd, timeout=timeout, capture_output=True, text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    print(output, end="", flush=True)

    sync_result = {"entries_updated": None, "entries_fetched": None, "full_pull": None}
    for line in output.splitlines():
        if line.startswith("SYNC_RESULT: "):
            try:
                sync_result.update(json.loads(line[len("SYNC_RESULT: "):]))
            except json.JSONDecodeError:
                pass

    return {"ok": result.returncode == 0, **sync_result}


def _start_log(sync_type: str, trigger: str) -> int:
    """INSERT the 'running' row for this pipeline run; returns its id. Steps get filled
    in incrementally via _update_log_steps as the run progresses, so a status/log poll
    mid-run sees live per-step progress rather than nothing until the run finishes.

    sync_type is 'full_sync' for a normal run or 'force_full_resync' when
    FORCE_FULL_RESYNC is set (issue #20) — recorded once here and never overwritten by
    _finish_log()'s later UPDATE.

    trigger is 'manual' (Sync Now / Force Full Resync button) or 'scheduled' (the
    daily APScheduler loop) — issue #46, threaded in from app/main.py via the
    TRIGGER env var so the sync-history UI can distinguish the two."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sync_log (user_id, type, status, steps, trigger) VALUES (%s, %s, %s, %s, %s) "
                "RETURNING id",
                (USER_ID, sync_type, "running", "[]", trigger),
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


_EMPTY_STEP_RESULT = {"entries_updated": None, "error_msg": None, "full_pull": None, "entries_fetched": None}


def _run_step(log_id: int, steps: list[dict], service: str, fn) -> str:
    """Run one step's fn() -> {"status", "entries_updated", "error_msg", "full_pull",
    "entries_fetched"}, recording a live 'running' placeholder before it starts (so a
    mid-run poll sees which step is currently in flight) and the real result once it
    finishes. An unexpected exception inside fn() is caught here and recorded as that
    step's error instead of crashing the whole pipeline — this is what makes steps
    independent."""
    steps.append({"service": service, "status": "running", **_EMPTY_STEP_RESULT})
    _update_log_steps(log_id, steps)
    try:
        result = fn()
    except Exception as e:
        result = {"status": "error", **_EMPTY_STEP_RESULT, "error_msg": f"Unexpected error: {e}"}
    steps[-1] = {"service": service, **result}
    _update_log_steps(log_id, steps)
    if result["status"] == "error":
        log(f"ERROR: {service} — {result['error_msg']}")
    return result["status"]


def _do_crunchyroll(cr_etp_rt: str, credentials_env: dict, force_full_resync: bool = False) -> dict:
    if not cr_etp_rt:
        log("Crunchyroll — no ETP-RT configured, skipping")
        return {"status": "skipped", **_EMPTY_STEP_RESULT}

    log("Crunchyroll — syncing → AniList" + (" (forced full resync)" if force_full_resync else ""))
    extra_env = {**credentials_env, "CRUNCHYROLL_ETP_RT": cr_etp_rt}
    if force_full_resync:
        extra_env["FORCE_FULL_RESYNC"] = "1"
    result = run(
        [sys.executable, str(SCRIPTS_DIR / "sync_crunchyroll.py")],
        extra_env=extra_env,
        timeout=PROVIDER_STEP_TIMEOUT,
    )
    if not result["ok"]:
        return {"status": "error", **_EMPTY_STEP_RESULT, "error_msg": "Crunchyroll → AniList sync failed"}

    return {"status": "ok", "entries_updated": result["entries_updated"], "error_msg": None,
            "full_pull": result["full_pull"], "entries_fetched": result["entries_fetched"]}


def _do_netflix(
    netflix_cookie_header: str,
    netflix_profile_guid: str,
    credentials_env: dict,
    force_full_resync: bool = False,
) -> dict:
    if not (netflix_cookie_header and netflix_profile_guid):
        log("Netflix — no credentials configured, skipping")
        return {"status": "skipped", **_EMPTY_STEP_RESULT}

    log("Netflix — syncing → AniList" + (" (forced full resync)" if force_full_resync else ""))
    extra_env = {
        **credentials_env,
        "NETFLIX_COOKIE_HEADER": netflix_cookie_header,
        "NETFLIX_PROFILE_GUID": netflix_profile_guid,
    }
    if force_full_resync:
        extra_env["FORCE_FULL_RESYNC"] = "1"
    result = run(
        [sys.executable, str(SCRIPTS_DIR / "sync_netflix.py")],
        extra_env=extra_env,
        timeout=PROVIDER_STEP_TIMEOUT,
    )
    if not result["ok"]:
        return {"status": "error", **_EMPTY_STEP_RESULT, "error_msg": "Netflix → AniList sync failed"}

    return {"status": "ok", "entries_updated": result["entries_updated"], "error_msg": None,
            "full_pull": result["full_pull"], "entries_fetched": result["entries_fetched"]}


def _do_plex(plex_server_token: str, plex_server_base_url: str, credentials_env: dict,
             force_full_resync: bool = False) -> dict:
    if not (plex_server_token and plex_server_base_url):
        log("Plex — not connected, skipping")
        return {"status": "skipped", **_EMPTY_STEP_RESULT}

    log("Plex — syncing → AniList" + (" (forced full resync)" if force_full_resync else ""))
    extra_env = {
        **credentials_env,
        "PLEX_SERVER_TOKEN": plex_server_token,
        "PLEX_SERVER_BASE_URL": plex_server_base_url,
    }
    if force_full_resync:
        extra_env["FORCE_FULL_RESYNC"] = "1"
    result = run(
        [sys.executable, str(SCRIPTS_DIR / "sync_plex.py")],
        extra_env=extra_env,
        timeout=PROVIDER_STEP_TIMEOUT,
    )
    if not result["ok"]:
        return {"status": "error", **_EMPTY_STEP_RESULT, "error_msg": "Plex → AniList sync failed"}

    return {"status": "ok", "entries_updated": result["entries_updated"], "error_msg": None,
            "full_pull": result["full_pull"], "entries_fetched": result["entries_fetched"]}


def _do_primevideo(primevideo_cookie_header: str, credentials_env: dict,
                    force_full_resync: bool = False) -> dict:
    if not primevideo_cookie_header:
        log("Prime Video — no credentials configured, skipping")
        return {"status": "skipped", **_EMPTY_STEP_RESULT}

    log("Prime Video — syncing → AniList" + (" (forced full resync)" if force_full_resync else ""))
    extra_env = {**credentials_env, "PRIMEVIDEO_COOKIE_HEADER": primevideo_cookie_header}
    if force_full_resync:
        extra_env["FORCE_FULL_RESYNC"] = "1"
    result = run(
        [sys.executable, str(SCRIPTS_DIR / "sync_primevideo.py")],
        extra_env=extra_env,
        timeout=PROVIDER_STEP_TIMEOUT,
    )
    if not result["ok"]:
        return {"status": "error", **_EMPTY_STEP_RESULT, "error_msg": "Prime Video → AniList sync failed"}

    return {"status": "ok", "entries_updated": result["entries_updated"], "error_msg": None,
            "full_pull": result["full_pull"], "entries_fetched": result["entries_fetched"]}


def _do_anilist_postgres(credentials_env: dict) -> dict:
    log("AniList — syncing → Postgres")
    result = run(
        [sys.executable, str(SCRIPTS_DIR / "sync_anilist.py")],
        extra_env=credentials_env,
        timeout=ANILIST_STEP_TIMEOUT,
    )
    if not result["ok"]:
        return {"status": "error", **_EMPTY_STEP_RESULT, "error_msg": "AniList → Postgres sync failed"}

    return {"status": "ok", "entries_updated": result["entries_updated"], "error_msg": None,
            "full_pull": result["full_pull"], "entries_fetched": result["entries_fetched"]}


def _compute_overall_status(steps: list[dict]) -> str:
    anilist_status = next((s["status"] for s in steps if s["service"] == "anilist_postgres"), None)
    if anilist_status != "ok":
        return "error"
    if any(s["status"] == "error" for s in steps if s["service"] != "anilist_postgres"):
        return "partial"
    return "ok"


def main() -> None:
    log("Starting full sync pipeline")
    # Issue #20 (Crunchyroll) / #21 (Netflix) — see sync_crunchyroll.py's and
    # sync_netflix.py's own FORCE_FULL_RESYNC handling. AniList is unaffected — its
    # step always does a fresh full read of the current AniList list state.
    force_full_resync = os.environ.get("FORCE_FULL_RESYNC", "").strip().lower() in ("1", "true", "yes")
    sync_type = "force_full_resync" if force_full_resync else "full_sync"
    # Issue #46 — set by app/main.py's _run_sync_task (defaults to "manual" for the
    # Sync Now / Force Full Resync buttons; "scheduled" for the daily APScheduler loop).
    trigger = os.environ.get("TRIGGER", "manual")
    log_id = _start_log(sync_type, trigger)
    settings = load_settings()

    # Resolve credentials: DB settings take priority over env vars (env var fallback
    # only meaningful for local dev/testing without a real settings row)
    anilist_token         = settings.get("anilist_token")         or os.environ.get("ANILIST_TOKEN", "")
    anilist_username      = settings.get("anilist_username")      or os.environ.get("ANILIST_USERNAME", "")
    cr_etp_rt             = settings.get("cr_etp_rt")             or os.environ.get("CRUNCHYROLL_ETP_RT", "")
    netflix_cookie_header  = settings.get("netflix_cookie_header")  or os.environ.get("NETFLIX_COOKIE_HEADER", "")
    netflix_profile_guid   = settings.get("netflix_profile_guid")   or os.environ.get("NETFLIX_PROFILE_GUID", "")
    plex_server_token     = settings.get("plex_server_token")     or os.environ.get("PLEX_SERVER_TOKEN", "")
    plex_server_base_url  = settings.get("plex_server_base_url")  or os.environ.get("PLEX_SERVER_BASE_URL", "")
    primevideo_cookie_header = settings.get("primevideo_cookie_header") or os.environ.get("PRIMEVIDEO_COOKIE_HEADER", "")

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

    # Issue #99 — anilist_postgres now runs first, not last: crunchyroll/netflix
    # matching reads AniList's list from the local library_entries mirror instead of
    # each independently making its own live fetch_user_list() call, so that mirror
    # needs to be fresh before they run. _compute_overall_status/_finish_log below key
    # off steps[i]["service"] by name, not position, so this reorder doesn't affect
    # how overall status or entries_updated get computed.
    steps: list[dict] = []
    _run_step(log_id, steps, "anilist_postgres", lambda: _do_anilist_postgres(credentials_env))
    _run_step(log_id, steps, "crunchyroll", lambda: _do_crunchyroll(cr_etp_rt, credentials_env, force_full_resync))
    _run_step(log_id, steps, "netflix", lambda: _do_netflix(netflix_cookie_header, netflix_profile_guid, credentials_env, force_full_resync))
    _run_step(log_id, steps, "plex", lambda: _do_plex(plex_server_token, plex_server_base_url, credentials_env, force_full_resync))
    _run_step(log_id, steps, "primevideo", lambda: _do_primevideo(primevideo_cookie_header, credentials_env, force_full_resync))

    overall_status = _compute_overall_status(steps)
    anilist_step = next(s for s in steps if s["service"] == "anilist_postgres")
    top_error_msg = anilist_step["error_msg"] if overall_status == "error" else None
    # Issue #46 — real "entries touched this run" across all three channels, not just
    # the AniList step's own count (and no longer the AniList library's total row
    # count either). Skipped steps (no credentials configured) contribute nothing;
    # an errored step's entries_updated is always None so also contributes 0.
    entries_updated = sum(s["entries_updated"] or 0 for s in steps if s["status"] != "skipped")
    _finish_log(log_id, overall_status, entries_updated, top_error_msg, steps)

    log(f"Full sync pipeline complete — overall status: {overall_status}")
    sys.exit(0 if overall_status in ("ok", "partial") else 1)


if __name__ == "__main__":
    main()
