import base64
import html
import io
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import bcrypt
import httpx
import psycopg2.extras
import pyotp
import qrcode
import qrcode.image.svg
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from authlib.integrations.starlette_client import OAuth
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

log = logging.getLogger("anime_tracker")

load_dotenv()

from app import db, config, privacy, outbox, i18n, sessions, credential_check, pat, mcp_server
from app.notify import DISCORD_WEBHOOK_RE, notify, ntfy_host_blocked

def _get_anilist_token(user_id: int) -> str:
    """Return this user's AniList token from settings DB."""
    return config.get(user_id, "anilist_token")

# ── Sync orchestration ────────────────────────────────────────────────────────
# Sync is per-user: each user has their own AniList account/credentials, so both the
# manual "Sync Now" trigger and the scheduled job operate on one user_id at a time.
# The schedule TIME itself (when the daily cron fires) stays instance-wide — there's
# exactly one cron trigger regardless of how many users exist, configured via
# instance_config rather than per-user settings (see _apply_schedule below).
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
_FULL_SYNC_SCRIPT = os.path.join(_SCRIPTS_DIR, "run_full_sync.py")
_RECOMMENDER_SCRIPT = os.path.join(_SCRIPTS_DIR, "run_recommender.py")
_AIRING_SCHEDULE_SCRIPT = os.path.join(_SCRIPTS_DIR, "sync_airing_schedule.py")
_FILLER_DATA_SCRIPT = os.path.join(_SCRIPTS_DIR, "sync_filler_data.py")
_NETFLIX_CSV_IMPORT_SCRIPT = os.path.join(_SCRIPTS_DIR, "import_netflix_csv.py")
_NETFLIX_CSV_IMPORT_TIMEOUT = 480  # seconds — same order as PROVIDER_STEP_TIMEOUT in
                                    # run_full_sync.py; a full-history CSV runs the same
                                    # per-title AniList search fallback the live path does

_sync_lock = threading.Lock()
# user_id -> {"running": bool, "last_result": str|None}. Only used as the double-click
# guard on POST /api/sync and to bridge the brief startup race before run_full_sync.py's
# subprocess manages to INSERT its own 'running' sync_log row — GET /api/sync/status
# and GET /api/sync/log read display data (status/steps) straight from that row instead,
# since it's updated live as the pipeline runs (see scripts/run_full_sync.py, issue #62).
_sync_state: dict[int, dict] = {}

_scheduler = BackgroundScheduler(timezone="UTC")


def _get_sync_state(user_id: int) -> dict:
    return _sync_state.setdefault(user_id, {"running": False, "last_result": None})


def _close_out_orphaned_log(user_id: int, log_type: str, message: str) -> None:
    """The subprocess was killed (or never started) before it could run its own
    _finish_log(), so the sync_log row it INSERTed via _start_log() would otherwise be
    left orphaned at status='running' forever (issue #91). Close it out here so the UI
    and history reflect reality instead of a permanently-stuck row.

    Scoped to this user's row of this specific `log_type` + status='running' — since
    issue #84, sync_log holds more than one job type per user (full_sync/
    force_full_resync/recommender), so a type-less filter here would risk closing out
    the wrong job's concurrently-running row for the same user instead of this one's.
    """
    try:
        db.execute(
            "UPDATE sync_log SET status = 'error', error_msg = %s "
            "WHERE user_id = %s AND type = %s AND status = 'running'",
            (message[:800], user_id, log_type),
        )
    except Exception as db_e:
        log.error("Could not close out orphaned %s sync_log row for user %s: %s", log_type, user_id, db_e)


def _run_sync_task(
    user_id: int, script: str = _FULL_SYNC_SCRIPT, force_full_resync: bool = False, trigger: str = "manual"
) -> None:
    """trigger is 'manual' (Sync Now / Force Full Resync buttons) or 'scheduled' (the
    daily APScheduler loop via _scheduled_full_sync) — issue #46, threaded through to
    run_full_sync.py via the TRIGGER env var so sync_log can distinguish the two."""
    state = _get_sync_state(user_id)
    env = os.environ.copy()
    env["USER_ID"] = str(user_id)
    env["TRIGGER"] = trigger
    if force_full_resync:
        env["FORCE_FULL_RESYNC"] = "1"
    run_started_at = datetime.now(timezone.utc)
    try:
        result = subprocess.run(
            # Issue #91 — this is now a last-resort safety net, not the primary timeout
            # enforcement: run_full_sync.py gives each step (crunchyroll/netflix/
            # anilist_postgres) its own timeout internally, so a slow step no longer
            # starves the others. This outer ceiling comfortably covers the sum of all
            # per-step budgets plus interpreter/DB-connect overhead, and only fires if
            # something goes more wrong than a single step running long.
            [sys.executable, script],
            capture_output=True, text=True, timeout=1500, env=env,
        )
        if result.returncode == 0:
            state["last_result"] = "ok"
            log.info("Sync completed for user %s: %s", user_id, script)
        else:
            state["last_result"] = "error"
            log.error("Sync failed for user %s (%s) stderr: %s", user_id, script, result.stderr[-800:])
    except Exception as e:
        state["last_result"] = "error"
        log.error("Sync exception for user %s (%s): %s", user_id, script, e)
        sync_type = "force_full_resync" if force_full_resync else "full_sync"
        _close_out_orphaned_log(user_id, sync_type, f"Sync did not complete: {e}")
    finally:
        state["running"] = False

    # Issue #11 — one alert per run, covering both manual (Sync Now / Force Full
    # Resync) and scheduled triggers since they all funnel through this function.
    # Wrapped separately so a failure in the alerting path itself (e.g. a transient DB
    # error on the lookup below) can never look like "no notification was needed."
    try:
        _notify_sync_outcome(user_id, force_full_resync, run_started_at)
    except Exception as e:
        log.error("Failed to send sync-outcome notification for user %s: %s", user_id, e)

    # Issue #229 — only worth checking if anilist_postgres itself actually refreshed
    # `anime`/`library_entries` this run; state["last_result"] == "ok" reflects
    # run_full_sync.py's exit code, which is 0 for both an "ok" and a "partial"
    # overall status (partial only ever means a *provider* step — crunchyroll/netflix
    # — failed, not anilist_postgres) and 1 when anilist_postgres itself failed. See
    # run_full_sync.py's _compute_overall_status. Wrapped separately for the same
    # reason as the block above — a failure here must never look like a skipped check.
    if state["last_result"] == "ok":
        try:
            _check_streaming_availability(user_id)
        except Exception as e:
            log.error("Streaming-availability check failed for user %s: %s", user_id, e)


def _notify_sync_outcome(user_id: int, force_full_resync: bool, run_started_at: datetime) -> None:
    """Alert the user on a genuine sync failure — distinct from an expected per-step
    skip (e.g. no Crunchyroll credentials configured), which run_full_sync.py already
    tags separately in sync_log.steps rather than as an error.

    Reads the sync_log row this run itself just wrote instead of trusting the
    subprocess return code: a 'partial' run (one step genuinely failed, but the
    AniList/Postgres step itself still succeeded) exits 0, so the return code alone
    would misreport it as a plain success. run_started_at guards against picking up a
    stale row from a previous run if this run crashed before writing its own (e.g. the
    subprocess couldn't even connect to Postgres to start logging).
    """
    sync_type = "force_full_resync" if force_full_resync else "full_sync"
    row = db.fetchone(
        "SELECT status, error_msg, steps, run_at FROM sync_log "
        "WHERE user_id = %s AND type = %s ORDER BY id DESC LIMIT 1",
        (user_id, sync_type),
    )
    if not row or row["run_at"] < run_started_at:
        notify(user_id, "❌ Sync failed", "Anime Tracker — sync failed before it could record results. Check container logs.")
        return

    if row["status"] == "ok":
        notify(user_id, "✅ Sync completed", "Anime Tracker — sync completed successfully.")
        return

    failed_steps = [s for s in (row["steps"] or []) if s.get("status") == "error"]
    steps_text = "\n".join(f"- {s['service']}: {s.get('error_msg') or 'failed'}" for s in failed_steps) or None

    if row["status"] == "partial":
        # anilist_postgres itself succeeded — library data is current, only a
        # secondary source (Crunchyroll/Netflix) needs attention. Distinct title from
        # a total failure so this doesn't read as "your library data is stale."
        body = (f"One or more sync steps failed, but your library data is up to date:\n{steps_text}"
                if steps_text else (row["error_msg"] or "Anime Tracker — sync partially failed. Check container logs."))
        notify(user_id, "⚠️ Sync partially failed", body)
    else:
        body = (f"One or more sync steps failed:\n{steps_text}"
                if steps_text else (row["error_msg"] or "Anime Tracker — sync failed. Check container logs."))
        notify(user_id, "❌ Sync failed", body)


def _check_streaming_availability(user_id: int) -> None:
    """Issue #229 — notify when a Planning-list title gains its first AniList
    `externalLinks` streaming entry. Poll+diff over the community-curated
    `external_links` data every AniList sync already writes onto `anime` — no new
    external dependency, no changes to the sync scripts themselves; this runs
    entirely from the app side, reading whatever the sync that just finished wrote.

    State lives in `planning_availability_state` (migration 022) rather than being
    derived from some other stored snapshot — see that migration's header. A title
    seen for the very first time is only ever recorded as a baseline, never notified:
    a Planning title that already had availability the first time this check ever
    runs isn't a "gained" event, matching the issue's explicit "no notification for
    titles that already had availability" scope. Once a baseline exists, a
    false -> true flip fires exactly one notification; true -> false (a link
    disappearing again) is still recorded so a later re-gain is treated as a fresh
    transition rather than being silently swallowed forever by a fire-once-ever flag.
    """
    rows = db.fetchall(
        """
        SELECT le.anime_id, a.title_english, a.title_romaji, a.external_links,
               pas.had_availability
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        LEFT JOIN planning_availability_state pas
            ON pas.user_id = le.user_id AND pas.anime_id = le.anime_id
        WHERE le.user_id = %s AND le.status = 'PLANNING'
        """,
        (user_id,),
    )
    for row in rows:
        has_link = any(
            lnk.get("site") in STREAMING_SITES for lnk in (row["external_links"] or [])
        )
        had_availability = row["had_availability"]

        if had_availability is None:
            # First time we've ever checked this (user, anime) pair — just record the
            # baseline, don't notify (see docstring).
            db.execute(
                "INSERT INTO planning_availability_state (user_id, anime_id, had_availability) "
                "VALUES (%s, %s, %s) ON CONFLICT (user_id, anime_id) DO NOTHING",
                (user_id, row["anime_id"], has_link),
            )
            continue

        if has_link == had_availability:
            continue  # no transition this sync

        if has_link:  # false -> true: the notification-worthy transition
            title = row["title_english"] or row["title_romaji"]
            notify(
                user_id,
                "📺 Now streaming",
                f"{title} just gained streaming availability — it's on your Planning list.",
            )
            db.execute(
                "UPDATE planning_availability_state SET had_availability = true, notified_at = now(), "
                "updated_at = now() WHERE user_id = %s AND anime_id = %s",
                (user_id, row["anime_id"]),
            )
        else:  # true -> false: record it, no notification
            db.execute(
                "UPDATE planning_availability_state SET had_availability = false, updated_at = now() "
                "WHERE user_id = %s AND anime_id = %s",
                (user_id, row["anime_id"]),
            )


def _notify_if_planning_uncovered(user_id: int, anime_id: int) -> None:
    """Issue #287: alert when a title just moved TO Planning isn't covered by any
    streaming service the user owns (`user_streaming_services`, added by #284).

    Event-driven, not a scan: every call site below only invokes this once, right
    after a write it just made transitioned a row's status to 'PLANNING' from
    something else (or created a brand-new Planning row) — the caller is
    responsible for that "did this write actually change something" check, so this
    function itself never needs a notified-once ledger table the way
    notified_episodes/planning_availability_state do for their own poll-and-diff
    checks. Per the issue's explicit out-of-scope note, there is deliberately no
    retroactive scan of the existing Planning list — this only ever fires from a
    live status-change write path.

    Silently does nothing if the title has no known streaming availability at all
    (STREAMING_SITES ∩ external_links is empty) — there's nothing to name as an
    alternative, and issue #229's separate "gained availability" check already
    covers a title that later picks up its first streaming link.
    """
    row = db.fetchone(
        "SELECT title_english, title_romaji, external_links FROM anime WHERE id = %s",
        (anime_id,),
    )
    if not row:
        return

    available_on = {
        lnk.get("site") for lnk in (row["external_links"] or [])
        if lnk.get("site") in STREAMING_SITES
    }
    if not available_on:
        return

    owned = {
        r["service"] for r in db.fetchall(
            "SELECT service FROM user_streaming_services WHERE user_id = %s", (user_id,)
        )
    }
    if available_on & owned:
        return  # covered by at least one service the user already owns

    title = row["title_english"] or row["title_romaji"]
    services = ", ".join(sorted(available_on))
    notify(
        user_id,
        "📺 Not on your services",
        f"{title} was added to Planning, but it isn't covered by any streaming "
        f"service you own. It's available on: {services}.",
    )


def _users_with_sync_credentials() -> list[dict]:
    """Users who've configured an AniList token — eligible for scheduled sync."""
    return db.fetchall(
        """
        SELECT DISTINCT u.id
        FROM users u
        JOIN settings s ON s.user_id = u.id
        WHERE s.key = 'anilist_token' AND s.value != ''
        """
    )


def _refresh_airing_schedule() -> None:
    """Hourly job: refresh airing_schedule_cache once for the union of every user's
    WATCHING/PLANNING RELEASING anime — a global table, so this replaces what used
    to be a per-user step inside each sync (redundant re-fetching whenever two users
    shared a currently-airing show, and a lock/deadlock risk if their syncs happened
    to overlap). Runs on its own hourly cadence rather than being chained onto the
    daily full sync, so anime added via a manual sync or "Add Anime" outside that
    window still get picked up within the hour — offset to minute=45 so a fresh
    cache is in place before _check_airing_episodes' minute=0 run each hour.
    """
    try:
        result = subprocess.run(
            [sys.executable, _AIRING_SCHEDULE_SCRIPT],
            capture_output=True, text=True, timeout=300, env=os.environ.copy(),
        )
        if result.returncode != 0:
            log.error("Airing schedule refresh failed: %s", result.stderr[-800:])
    except Exception as e:
        log.error("Airing schedule refresh exception: %s", e)


def _refresh_filler_data() -> None:
    """Daily job: refresh filler_episode_cache/filler_sync_state/filler_data_license
    from AniFillerPedia (issue #299) — a global table like airing_schedule_cache, one
    pass over the whole catalog rather than per-user. Filler/canon status barely
    changes once approved, so this runs far less often than the hourly airing-schedule
    refresh; daily is plenty, and scripts/sync_filler_data.py's own per-title
    last-checked tracking means most runs after the first do very little work anyway.
    """
    try:
        result = subprocess.run(
            [sys.executable, _FILLER_DATA_SCRIPT],
            capture_output=True, text=True, timeout=1200, env=os.environ.copy(),
        )
        if result.returncode != 0:
            log.error("Filler data refresh failed: %s", result.stderr[-800:])
    except Exception as e:
        log.error("Filler data refresh exception: %s", e)


def _check_airing_episodes() -> None:
    """Hourly job: notify each user about their own unwatched episodes that started airing.

    Notification state is tracked per-user in notified_episodes rather than as a single
    flag on the shared airing_schedule_cache row — that table is global cache data (an
    airing time doesn't differ per user), so a global "notified" flag meant two users
    watching the same currently-airing show would race on it and only whichever user's
    loop iteration ran first ever got notified.
    """
    for user in db.fetchall("SELECT id FROM users"):
        user_id = user["id"]
        rows = db.fetchall(
            """
            SELECT a.title_english, a.title_romaji, asc_.anime_id, asc_.episode, asc_.airing_at
            FROM airing_schedule_cache asc_
            JOIN anime a ON a.id = asc_.anime_id
            JOIN library_entries le ON le.anime_id = asc_.anime_id
            LEFT JOIN notified_episodes ne
                ON ne.user_id = le.user_id AND ne.anime_id = asc_.anime_id AND ne.episode = asc_.episode
            WHERE ne.user_id IS NULL
              AND asc_.airing_at <= now()
              AND le.status IN ('WATCHING', 'PLANNING')
              AND le.user_id = %s
            ORDER BY asc_.airing_at
            """,
            (user_id,),
        )
        if not rows:
            continue
        lines = []
        for r in rows:
            title = r["title_english"] or r["title_romaji"]
            lines.append(f"▶ {title} — Ep {r['episode']} is now airing")
        notify(user_id, "New episode(s) airing", "\n".join(lines))

        for r in rows:
            db.execute(
                """
                INSERT INTO notified_episodes (user_id, anime_id, episode)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (user_id, r["anime_id"], r["episode"]),
            )


def _weekly_airing_digest() -> None:
    """Monday morning digest: each user's own upcoming episodes in the next 7 days."""
    for user in db.fetchall("SELECT id FROM users"):
        user_id = user["id"]
        rows = db.fetchall(
            """
            SELECT a.title_english, a.title_romaji, asc_.episode, asc_.airing_at
            FROM airing_schedule_cache asc_
            JOIN anime a ON a.id = asc_.anime_id
            JOIN library_entries le ON le.anime_id = asc_.anime_id
            WHERE asc_.airing_at BETWEEN now() AND now() + INTERVAL '7 days'
              AND le.status IN ('WATCHING', 'PLANNING')
              AND le.user_id = %s
            ORDER BY asc_.airing_at
            """,
            (user_id,),
        )
        if not rows:
            continue
        lines = []
        for r in rows:
            title = r["title_english"] or r["title_romaji"]
            dt = r["airing_at"].strftime("%a %d %b %H:%M UTC") if r["airing_at"] else ""
            lines.append(f"• {title} — Ep {r['episode']} ({dt})")
        notify(user_id, "Anime this week", "\n".join(lines))


# Issue #196 — periodic nudge pointing users at the living "year so far" page
# (/stats/wrapped), via #51's existing notify() fan-out. Timing choice, documented
# here since the issue explicitly asked for the reasoning to be written down:
#
# A single once-a-year "reveal" notification (the natural choice for a static
# year-END recap) doesn't fit a page whose whole point is that it's ALWAYS
# current — there's no one moment where it "unlocks". Two occasions instead:
#   - Mid-year check-in (Jul 1): the most natural "how's my year going" moment —
#     half the year's data is in, genre/pace/binge-week are all meaningfully
#     populated by then, not just a handful of January completions.
#   - Pre-year-end reminder (Dec 20): while there's still real runway (~11 days)
#     to finish something before the year closes and #wrapup-card's comparison
#     view takes over as the more useful lens — a Dec 31 ping would arrive too
#     late to act on.
# Both skip a user with zero completions so far this year (has_data is False) —
# nudging someone toward an empty page reads as noise, not a check-in.
_WRAPPED_CHECKIN_OCCASIONS = ("midyear", "year_end")


def _notify_wrapped_checkin(occasion: str) -> None:
    # Fail loudly on an unexpected occasion rather than a bare `else` silently
    # treating anything-not-"midyear" as "year_end" — a future scheduler-wiring
    # typo (e.g. "mid_year") should surface as an error, not send the wrong
    # message to every user without a trace of the mistake.
    if occasion not in _WRAPPED_CHECKIN_OCCASIONS:
        raise ValueError(f"Unknown wrapped check-in occasion: {occasion!r}")

    for user in db.fetchall("SELECT id FROM users"):
        user_id = user["id"]
        try:
            wrapped = _compute_wrapped_page(user_id)
            if not wrapped["has_data"]:
                continue
            if occasion == "midyear":
                body = (
                    f"You're {wrapped['total_episodes']} episodes into {wrapped['year']} so far — "
                    "see your top genre, biggest binge week, and pace vs. last year at /stats/wrapped."
                )
            else:  # "year_end"
                body = (
                    f"{wrapped['year']} is almost over — see how your year stacked up at /stats/wrapped."
                )
            notify(user_id, "📊 Your year so far", body)
        except Exception as e:
            log.error("Wrapped %s check-in notification failed for user %s: %s", occasion, user_id, e)


def _scheduled_wrapped_midyear_checkin() -> None:
    _notify_wrapped_checkin("midyear")


def _scheduled_wrapped_year_end_reminder() -> None:
    _notify_wrapped_checkin("year_end")


def _scheduled_full_sync() -> None:
    """Loop every user with sync credentials configured. One user's failure is caught
    and logged to that user's own sync_log, not allowed to stop anyone else's sync."""
    for user in _users_with_sync_credentials():
        user_id = user["id"]
        state = _get_sync_state(user_id)
        with _sync_lock:
            if state["running"]:
                log.warning("Scheduled sync skipped for user %s — already running", user_id)
                continue
            state["running"] = True
            state["last_result"] = None
        try:
            _run_sync_task(user_id, _FULL_SYNC_SCRIPT, trigger="scheduled")
        except Exception as e:
            log.error("Unhandled error syncing user %s: %s", user_id, e)
            state["running"] = False
            state["last_result"] = "error"


def _run_recommender_task(user_id: int) -> None:
    env = os.environ.copy()
    env["USER_ID"] = str(user_id)
    run_started_at = datetime.now(timezone.utc)
    try:
        result = subprocess.run(
            [sys.executable, _RECOMMENDER_SCRIPT],
            capture_output=True, text=True, timeout=600, env=env,
        )
        if result.returncode != 0:
            log.error("Recommender failed for user %s: %s", user_id, result.stderr[-800:])
    except Exception as e:
        log.error("Recommender exception for user %s: %s", user_id, e)
        _close_out_orphaned_log(user_id, "recommender", f"Recommender did not complete: {e}")

    # Issue #84 — one alert per run, parity with #11's sync-failure alerting.
    try:
        _notify_recommender_outcome(user_id, run_started_at)
    except Exception as e:
        log.error("Failed to send recommender-outcome notification for user %s: %s", user_id, e)


def _notify_recommender_outcome(user_id: int, run_started_at: datetime) -> None:
    """Alert the user only on a genuine recommender failure (issue #84). Unlike sync,
    which notifies on every run because it refreshes the whole library daily, the
    recommender only rebuilds the watch-next queue weekly, so a routine success isn't
    worth a notification — this only ever sends a failure alert.

    run_started_at guards against picking up a stale row from a previous run if this
    run crashed before writing its own (e.g. the subprocess couldn't even connect to
    Postgres to start logging) — same guard as _notify_sync_outcome.
    """
    row = db.fetchone(
        "SELECT status, error_msg, run_at FROM sync_log "
        "WHERE user_id = %s AND type = 'recommender' ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    if not row or row["run_at"] < run_started_at:
        notify(user_id, "❌ Recommender failed", "Anime Tracker — recommender run failed before it could record results. Check container logs.")
        return

    if row["status"] != "ok":
        body = row["error_msg"] or "Anime Tracker — recommender run failed. Check container logs."
        notify(user_id, "❌ Recommender failed", body)


def _scheduled_recommender() -> None:
    """Loop every user with sync credentials configured — same error isolation as sync."""
    for user in _users_with_sync_credentials():
        user_id = user["id"]
        log.info("Running scheduled recommender for user %s", user_id)
        try:
            _run_recommender_task(user_id)
        except Exception as e:
            log.error("Unhandled error running recommender for user %s: %s", user_id, e)


def _apply_schedule() -> None:
    """Read schedule from instance_config and (re)configure APScheduler jobs.

    Instance-wide, not per-user — there's exactly one cron trigger regardless of user
    count, which then loops over users itself. See _instance_config_get/set.
    """
    daily_time = _instance_config_get("sync_daily_time") or "04:30"
    rec_day    = _instance_config_get("sync_recommender_day") or "sun"
    rec_time   = _instance_config_get("sync_recommender_time") or "05:00"

    try:
        d_hour, d_min = daily_time.split(":")
        r_hour, r_min = rec_time.split(":")
    except ValueError:
        d_hour, d_min = "4", "30"
        r_hour, r_min = "5", "0"

    _scheduler.add_job(
        _scheduled_full_sync,
        CronTrigger(hour=d_hour, minute=d_min, timezone="UTC"),
        id="daily_sync", replace_existing=True,
    )
    _scheduler.add_job(
        _scheduled_recommender,
        CronTrigger(day_of_week=rec_day, hour=r_hour, minute=r_min, timezone="UTC"),
        id="weekly_recommender", replace_existing=True,
    )
    _scheduler.add_job(
        _refresh_airing_schedule,
        CronTrigger(minute=45, timezone="UTC"),
        id="airing_schedule_refresh", replace_existing=True,
    )
    _scheduler.add_job(
        _check_airing_episodes,
        CronTrigger(minute=0, timezone="UTC"),
        id="airing_check", replace_existing=True,
    )
    # Issue #299 — catalog-wide, not per-user, and not tied to instance_config's
    # user-facing sync-time settings (those are about each user's own AniList/CR/
    # Netflix sync). Fixed low-traffic UTC time, offset from every other job above
    # rather than reusing daily_sync's slot.
    _scheduler.add_job(
        _refresh_filler_data,
        CronTrigger(hour=3, minute=15, timezone="UTC"),
        id="filler_data_refresh", replace_existing=True,
    )
    _scheduler.add_job(
        _weekly_airing_digest,
        CronTrigger(day_of_week="mon", hour=7, minute=0, timezone="UTC"),
        id="weekly_digest", replace_existing=True,
    )
    # Issue #196 — "your year so far" (/stats/wrapped) check-in nudges. Fixed
    # calendar dates, instance-wide like every other cron trigger here — see
    # _notify_wrapped_checkin's docstring for why two dates (mid-year + pre-year-end)
    # rather than the single annual "reveal" a static recap would use.
    _scheduler.add_job(
        _scheduled_wrapped_midyear_checkin,
        CronTrigger(month=7, day=1, hour=9, minute=0, timezone="UTC"),
        id="wrapped_midyear_checkin", replace_existing=True,
    )
    _scheduler.add_job(
        _scheduled_wrapped_year_end_reminder,
        CronTrigger(month=12, day=20, hour=9, minute=0, timezone="UTC"),
        id="wrapped_year_end_reminder", replace_existing=True,
    )


def _next_run_time(job_id: str) -> str | None:
    """Next scheduled run for an APScheduler job, as an ISO string for the template.

    Shared by the Settings page (Sync & Credentials tab shows last-synced) and the
    Admin page (Instance Config tab's Sync Schedule form, since #96 moved it there) —
    both need the same daily_sync/weekly_recommender next-run times.
    """
    try:
        job = _scheduler.get_job(job_id)
        if job and job.next_run_time:
            return job.next_run_time.isoformat()
    except Exception:
        pass
    return None


ANILIST_API = "https://graphql.anilist.co"

# Dev/testing only — skips the live AniList push in rating/status/progress below so
# those endpoints are exercisable against compose/dev.yml's throwaway stack without a
# real ANILIST_TOKEN. Defaults off; never set outside compose/dev.yml.
ANILIST_MOCK = os.environ.get("ANILIST_MOCK") == "1"

SAVE_SCORE_MUTATION = """
mutation ($mediaId: Int!, $score: Float!) {
  SaveMediaListEntry(mediaId: $mediaId, score: $score) {
    id score
  }
}
"""

SAVE_STATUS_MUTATION = """
mutation ($mediaId: Int!, $status: MediaListStatus!) {
  SaveMediaListEntry(mediaId: $mediaId, status: $status) {
    id status
  }
}
"""

# Issue #100 — used by app/outbox.py's worker to deliver outbox rows, which may carry
# any non-empty subset of status/progress/repeat (a UI bulk-status edit only ever sets
# status; a Crunchyroll/Netflix-originated row may set progress alone, or progress
# together with status/repeat). SAVE_STATUS_MUTATION above stays as-is for the
# single-card synchronous endpoint, which only ever touches status.
SAVE_MEDIA_LIST_MUTATION = """
mutation ($mediaId: Int!, $progress: Int, $status: MediaListStatus, $repeat: Int) {
  SaveMediaListEntry(mediaId: $mediaId, progress: $progress, status: $status, repeat: $repeat) {
    id progress status repeat
  }
}
"""

SAVE_PROGRESS_MUTATION = """
mutation ($mediaId: Int!, $progress: Int!) {
  SaveMediaListEntry(mediaId: $mediaId, progress: $progress) {
    id progress
  }
}
"""

DELETE_MEDIA_LIST_ENTRY_MUTATION = """
mutation ($id: Int!) {
  DeleteMediaListEntry(id: $id) {
    deleted
  }
}
"""

# Fallback lookup for entries added via this app but not yet backfilled with
# anilist_entry_id by a sync run — resolves the viewer's own list-entry id by mediaId.
MEDIA_LIST_ENTRY_ID_QUERY = """
query ($mediaId: Int!) {
  Media(id: $mediaId) {
    mediaListEntry {
      id
    }
  }
}
"""

VALID_STATUSES = {"WATCHING", "COMPLETED", "DROPPED", "PLANNING", "PAUSED", "REPEATING"}
STATUS_TO_ANILIST = {"WATCHING": "CURRENT"}

# Issue #191 -- rewatch queue/reminder surface. Time-based trigger: a completed show
# whose most recent completion (`library_entries.finish_date`) is at least this many
# months in the past is surfaced as a rewatch candidate. `finish_date` is synced
# straight from AniList's own `completedAt` (see sync_anilist.py), which AniList
# updates every time an entry is (re)marked COMPLETED -- including finishing a
# rewatch -- so this single field already captures "time since last watched all the
# way through", original watch or rewatch alike, with no separate rewatch-recency
# check needed. Fixed threshold rather than a per-user setting, deliberately, to
# keep v1 simple (see issue #191 / #162's scoping notes); 6 months balances "long
# enough to plausibly want a rewatch" against "the section isn't dominated by every
# show ever finished".
REWATCH_REMINDER_MONTHS = 6


def rewatch_due(finish_date, months=REWATCH_REMINDER_MONTHS, today=None):
    """Pure trigger-logic helper for issue #191 -- kept separate from the /queue
    route so it's unit-testable without a database. Returns True when `finish_date`
    (a date or None) is far enough in the past to surface a rewatch reminder.
    30-day months, matching the "N months ago" display the UI derives from the same
    field -- deliberately approximate, not calendar-exact, consistent with how
    `months_since` is computed for display in the /queue route below."""
    if finish_date is None:
        return False
    today = today or date.today()
    return (today - finish_date).days >= months * 30

STREAMING_SITES = {
    "Crunchyroll", "Netflix", "Hulu", "Amazon Prime Video", "HIDIVE",
    "Disney Plus", "Bilibili TV", "Bilibili", "iQ", "WeTV", "Tubi TV",
    "Adult Swim", "Hoopla", "Max", "Tencent Video", "Bandai Channel",
    "Niconico Video", "Funimation", "VRV",
}

# Issue #231 — invite expiry window. A fresh invite (POST /admin/invites) and a
# resend (POST /admin/invites/{id}/resend) both push expires_at this far out from
# now(); the DB column carries the same default (schema.sql / migration 022) as a
# safety net for any direct INSERT that doesn't specify it. Not currently
# configurable per-instance — a flat 7 days is the implementation-time call #231's
# "Open questions" section left to whoever built it.
INVITE_EXPIRY_DAYS = 7

# Issue #218 — mood-at-log-time on personal notes, StoryGraph-inspired. A fixed
# picklist (not freeform, unlike personal_tags) so it stays useful for a future
# mood chart/filter (explicitly out of scope for #218 itself) without having to
# guess later which personal_tags entries were "really" moods. Ordered — this
# order drives both the notes-form checkbox order and every mood_* i18n key
# name below (t('mood_' + slug), mirroring the t('status_' + ...) convention).
# Validated in application code only, same as STREAMING_SITES above — see
# migrations/026_mood_tags.sql for why there's no DB-level CHECK.
MOOD_TAGS = [
    "comfort", "hype", "intense", "sad", "wholesome",
    "dark", "funny", "relaxing", "thought_provoking", "bittersweet",
]
_MOOD_TAGS_SET = set(MOOD_TAGS)


def _filter_mood_tags(raw) -> list:
    """Keep only recognized MOOD_TAGS values, in MOOD_TAGS order (not submission
    order) — dedupes for free and gives a stable display order regardless of how
    the caller (form checkboxes, JSON API, MCP tool) sent them. Unrecognized
    values are silently dropped rather than rejected: the UI only ever offers
    checkboxes for the fixed set, so a stray value only reaches here via the
    JSON API or MCP tool, most likely a stale client after MOOD_TAGS changes —
    not worth hard-failing the whole notes save over."""
    if not raw:
        return []
    submitted = {str(v).strip() for v in raw if str(v).strip()}
    return [m for m in MOOD_TAGS if m in submitted]

ANILIST_SEARCH_QUERY = """
query ($search: String) {
  Page(perPage: 8) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { romaji english }
      format
      seasonYear
      averageScore
      coverImage { large }
      status
    }
  }
}
"""

ANILIST_MEDIA_QUERY = """
query ($id: Int!) {
  Media(id: $id, type: ANIME) {
    id idMal
    title { romaji english native }
    format status episodes season seasonYear
    genres
    tags { name rank }
    studios { edges { isMain node { name } } }
    averageScore coverImage { large } bannerImage
    duration description(asHtml: false)
    trailer { id site }
    externalLinks { site url }
    streamingEpisodes { title url site thumbnail }
    relations { edges { relationType node { id title { romaji english } coverImage { large } format } } }
  }
}
"""


def _upsert_anime_row(media: dict) -> None:
    """Upsert a single AniList Media dict into the anime table."""
    studios = [
        {"name": e["node"]["name"], "isMain": e["isMain"]}
        for e in (media.get("studios") or {}).get("edges", [])
    ]
    tags = [
        {"name": t["name"], "rank": t["rank"]}
        for t in (media.get("tags") or [])
    ]
    ext_links = [
        {"site": lnk["site"], "url": lnk["url"]}
        for lnk in (media.get("externalLinks") or [])
    ]
    streaming = [
        {"title": ep["title"], "url": ep["url"], "site": ep["site"], "thumbnail": ep.get("thumbnail")}
        for ep in (media.get("streamingEpisodes") or [])
    ]
    trailer_raw = media.get("trailer") or {}
    trailer_yt_id = (
        trailer_raw.get("id") if trailer_raw.get("site", "").lower() == "youtube" else None
    )
    relations = [
        {
            "id": edge["node"]["id"],
            "title": (edge["node"].get("title") or {}).get("english")
                     or (edge["node"].get("title") or {}).get("romaji", ""),
            "cover": (edge["node"].get("coverImage") or {}).get("large"),
            "format": edge["node"].get("format"),
            "relation_type": edge.get("relationType", "OTHER"),
        }
        for edge in ((media.get("relations") or {}).get("edges") or [])
        if edge.get("node")
    ]
    db.execute(
        """
        INSERT INTO anime (
            id, id_mal, title_romaji, title_english, title_native,
            format, status, episodes, duration, season, season_year,
            genres, tags, studios, average_score,
            cover_image_url, banner_image_url, description,
            trailer_yt_id, external_links, streaming_episodes, relations, last_synced_at
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s, now()
        )
        ON CONFLICT (id) DO UPDATE SET
            id_mal             = EXCLUDED.id_mal,
            title_romaji       = EXCLUDED.title_romaji,
            title_english      = EXCLUDED.title_english,
            title_native       = EXCLUDED.title_native,
            format             = EXCLUDED.format,
            status             = EXCLUDED.status,
            episodes           = EXCLUDED.episodes,
            duration           = EXCLUDED.duration,
            season             = EXCLUDED.season,
            season_year        = EXCLUDED.season_year,
            genres             = EXCLUDED.genres,
            tags               = EXCLUDED.tags,
            studios            = EXCLUDED.studios,
            average_score      = EXCLUDED.average_score,
            cover_image_url    = EXCLUDED.cover_image_url,
            banner_image_url   = EXCLUDED.banner_image_url,
            description        = EXCLUDED.description,
            trailer_yt_id      = EXCLUDED.trailer_yt_id,
            external_links     = EXCLUDED.external_links,
            streaming_episodes = EXCLUDED.streaming_episodes,
            relations          = EXCLUDED.relations,
            last_synced_at     = now()
        """,
        (
            media["id"],
            media.get("idMal"),
            media["title"]["romaji"],
            media["title"].get("english"),
            media["title"].get("native"),
            media.get("format"),
            media.get("status"),
            media.get("episodes"),
            media.get("duration"),
            media.get("season"),
            media.get("seasonYear"),
            json.dumps(media.get("genres") or []),
            json.dumps(tags),
            json.dumps(studios),
            media.get("averageScore"),
            (media.get("coverImage") or {}).get("large"),
            media.get("bannerImage"),
            media.get("description"),
            trailer_yt_id,
            json.dumps(ext_links),
            json.dumps(streaming),
            json.dumps(relations),
        ),
    )
app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
# Issue #207 — MCP server, mounted as a route prefix of this same app rather than a
# separate process/container. See app/mcp_server.py's module docstring for the full
# reasoning; short version: it's read-only, has no background work of its own, and
# serves the same tables/DB connection this app already has, so it doesn't clear the
# bar this repo otherwise holds new containers to (see CLAUDE.md's "One sync path,
# not two" decision). Bearer-token auth is handled entirely inside mcp_server.asgi_app
# (app/pat.py's PATs) — nothing about /mcp goes through the cookie-session auth the
# rest of this app uses.
app.mount("/mcp", mcp_server.asgi_app)
templates = Jinja2Templates(directory="app/templates")


_SERVICE_WORKER_PATH = "app/static/service-worker.js"


@app.get("/service-worker.js")
def service_worker() -> FileResponse:
    # Served from the root path (not /static/service-worker.js) so its default
    # scope is "/" and it actually controls the app's pages — a service worker
    # registered from under /static/ can only ever control /static/* by default,
    # which fails the "has a service worker controlling start_url" PWA
    # installability check (#12). Minimal no-op-fetch worker, not for offline
    # caching — see the file's own header comment.
    #
    # FileResponse (not a hand-rolled open()+Response) so this gets correct
    # ETag/Last-Modified headers for free instead of reinventing them. Note
    # this Starlette version's plain FileResponse doesn't itself short-circuit
    # a matching If-None-Match into a 304 the way the /static mount's
    # StaticFiles does — it only sets the validator headers — but that's fine
    # here: Cache-Control: no-cache below means the browser always revalidates
    # before using a cached copy, so the freshness guarantee holds either way,
    # it just costs a full response body on revalidation instead of a 304.
    #
    # The explicit os.path.exists check (and 404) below is deliberate too:
    # FileResponse itself only stats the file lazily inside its ASGI __call__,
    # and turns a missing file into an unhandled RuntimeError (-> 500), not a
    # 404 — that auto-404 behavior belongs to StaticFiles' own lookup, not to
    # a bare FileResponse returned from a route.
    if not os.path.exists(_SERVICE_WORKER_PATH):
        raise HTTPException(status_code=404)
    return FileResponse(
        _SERVICE_WORKER_PATH,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )

_SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY")
if not _SESSION_SECRET_KEY:
    _SESSION_SECRET_KEY = secrets.token_hex(32)
    log.warning(
        "SESSION_SECRET_KEY not set — generated a random one for this process. "
        "Sessions will NOT survive a restart. Set SESSION_SECRET_KEY for production."
    )
# Cookie max_age matches sessions.SESSION_TTL_DAYS (issue #82) — the signed cookie
# and the server-side sessions row it points at expire on the same schedule, so
# neither one outlives the other in either direction.
app.add_middleware(
    SessionMiddleware,
    secret_key=_SESSION_SECRET_KEY,
    max_age=sessions.SESSION_TTL_DAYS * 24 * 60 * 60,
)

oauth = OAuth()  # clients registered dynamically per-request — see _ensure_oauth_registered

AUTH_PROVIDERS = {"google", "discord"}

_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_MINUTES = 15

# ── TOTP two-factor authentication (issue #83) ──────────────────────────────────
# Local-account-only, deliberately unrelated to the OAuth login/callback routes above
# (out of scope per the issue — Google/Discord already get the provider's own 2FA).
_TOTP_ISSUER = "Anime Tracker"  # matches the brand name shown everywhere else in the
                                # app's own UI (base.html's site-name/title suffix) —
                                # not the repo/package name, which differs (see
                                # CLAUDE.local.md), so an authenticator app entry
                                # doesn't show the user something they don't recognize
_TOTP_SETUP_TTL_MINUTES = 10
_TOTP_SETUP_MAX_ATTEMPTS = 10
_TOTP_RECOVERY_CODE_COUNT = 8
_PENDING_2FA_SESSION_KEY = "pending_2fa_user_id"

# A separate lockout counter from _LOGIN_MAX_ATTEMPTS/_LOGIN_LOCKOUT_MINUTES above —
# same numbers today, but deliberately its own users.totp_failed_attempts/
# totp_locked_until columns rather than sharing failed_login_attempts/locked_until
# with the password check. Security-review finding (post-#83): the password-reset
# flow (auth_reset_password_submit) resets failed_login_attempts/locked_until as a
# side effect of a successful reset, which is correct for the *password* guess
# budget but would also have silently re-armed an in-progress *2FA-code* brute-force
# lockout if the two shared a column — an attacker mid-way through guessing someone's
# TOTP code would get a fresh 5-attempt budget for free the moment an unrelated
# password reset happened. Splitting the columns means a password reset now simply
# can't touch the TOTP-code counter at all.
_TOTP_LOGIN_MAX_ATTEMPTS = 5
_TOTP_LOGIN_LOCKOUT_MINUTES = 15

# Pending (not-yet-confirmed) TOTP setup state, and the one-time recovery-code
# display payload, are held server-side in-process — keyed by user_id, never
# written to users.totp_secret/totp_recovery_codes until a real 6-digit code from
# the authenticator app has been verified (setup) or the codes have been shown
# once (recovery display).
#
# Security-review finding (post-#83): these were originally round-tripped through
# request.session instead. Starlette's SessionMiddleware cookie is itsdangerous-
# *signed*, not encrypted — signing proves the cookie wasn't tampered with, it does
# NOT keep the cookie's contents confidential. Anyone who can read the raw cookie
# value (a browser extension with cookie-read permission, a proxy/log system that
# captures full request/response headers) can base64-decode it and read a raw TOTP
# secret or all 8 recovery codes in plaintext, without ever needing this process's
# SESSION_SECRET_KEY. That's a materially different risk than the session's other
# use (an opaque user_id, which authorizes nothing on its own without the signed
# cookie itself) — nothing else in this app puts a real secret in request.session
# (verified via `grep -n 'request\.session\[' app/main.py` before making this call),
# so TOTP setup isn't going to be the first. Kept in-process (same pattern as
# _sync_state below) rather than a DB table since it's inherently short-lived
# (cleared on confirm/expiry/first display) and this app runs as a single uvicorn
# process with no --workers flag (see Dockerfile CMD) — the one tradeoff is that a
# mid-setup process restart drops the pending state, which just means the user sees
# a fresh QR code (GET regenerates one, see settings_2fa_setup_page) rather than any
# data loss or security gap.
_totp_setup_state: dict[int, dict] = {}   # user_id -> {"secret", "started_at", "attempts"}
_totp_recovery_display: dict[int, list[str]] = {}  # user_id -> plaintext codes, popped on render

# Same one-time-display convention as _totp_recovery_display above, for issue #207's
# personal access tokens: user_id -> {"name", "token"}, popped on render by
# settings_token_created. The raw token is never written to the database or the
# session cookie — see app/pat.py's module docstring for the storage approach.
_pat_display: dict[int, dict] = {}


def _generate_totp_recovery_codes() -> list[str]:
    """8 one-time backup codes (issue #83's lost-authenticator recovery mechanism),
    formatted like 'a1b2-c3d4' for readability. Only ever returned here for the
    one-time display right after enabling — callers must hash before persisting,
    see _hash_recovery_code."""
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(_TOTP_RECOVERY_CODE_COUNT)]


def _hash_recovery_code(code: str) -> str:
    """Same standard as users.password_hash — bcrypt, never stored plaintext. Unlike
    the other bcrypt.hashpw call sites in this file (registration, password reset,
    settings_set_password), this one also lower-cases and strips the input first —
    deliberate, not an oversight: recovery codes are meant to be enterable without
    worrying about case, so both hashing and verification (see
    _consume_recovery_code_if_valid) normalize the same way. Don't "fix" this
    inconsistency by dropping the normalization."""
    return bcrypt.hashpw(code.strip().lower().encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _consume_recovery_code_if_valid(user_id: int, code: str) -> int | None:
    """Checks `code` against this user's unused recovery-code hashes (bcrypt compare,
    since they're hashed at rest — no direct lookup possible). On a match, atomically
    marks that row used (the `WHERE used_at IS NULL` guard on the UPDATE closes the
    race window against a concurrent request replaying the same code) and returns its
    id, or None if used_at was already set by whoever won that race. Returns None if
    no row matches at all. One connection/transaction for the whole find-and-consume
    operation rather than two separate db.fetchall/db.execute_returning round trips."""
    code_norm = code.strip().lower().encode("utf-8")
    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, code_hash FROM totp_recovery_codes WHERE user_id = %s AND used_at IS NULL",
                (user_id,),
            )
            rows = cur.fetchall()
            for row in rows:
                if bcrypt.checkpw(code_norm, row["code_hash"].encode("utf-8")):
                    cur.execute(
                        "UPDATE totp_recovery_codes SET used_at = now() WHERE id = %s AND used_at IS NULL RETURNING id",
                        (row["id"],),
                    )
                    claimed = cur.fetchone()
                    conn.commit()
                    return claimed["id"] if claimed else None
    return None


def _totp_qr_data_uri(secret: str, email: str) -> str:
    """Renders the QR code as an inline base64 SVG data: URI — never written to disk,
    never served from a static/cacheable route, never passed as a URL query param that
    could end up in an access log. It only ever exists embedded directly in the HTML of
    the one-time setup screen's own HTTP response."""
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=_TOTP_ISSUER)
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _start_totp_setup_state(user_id: int) -> str:
    """Issues a fresh pending secret for user_id, overwriting any previous pending
    state (e.g. an expired attempt) — see settings_2fa_setup_state."""
    secret = pyotp.random_base32()
    _totp_setup_state[user_id] = {
        "secret": secret,
        "started_at": datetime.now(timezone.utc),
        "attempts": 0,
    }
    return secret


def _totp_setup_state_for(user_id: int) -> dict | None:
    """Returns the pending setup state for user_id if one exists and hasn't expired
    (_TOTP_SETUP_TTL_MINUTES) — expired state is discarded here so callers never see
    a stale secret. This is the single place both the GET (render) and POST
    (confirm) setup handlers check TTL, so they can no longer disagree about whether
    a given secret is still current (a QR the user is actively looking at on GET
    always matches what POST will accept, and vice versa)."""
    state = _totp_setup_state.get(user_id)
    if not state:
        return None
    if datetime.now(timezone.utc) - state["started_at"] > timedelta(minutes=_TOTP_SETUP_TTL_MINUTES):
        _totp_setup_state.pop(user_id, None)
        return None
    return state


def _clear_totp_setup_state(user_id: int) -> None:
    _totp_setup_state.pop(user_id, None)


@app.on_event("startup")
def startup() -> None:
    _apply_schedule()
    _scheduler.start()
    outbox.start_worker()
    log.info("APScheduler started")


@app.on_event("startup")
async def startup_mcp() -> None:
    # Issue #207 — must run before any request can reach mcp_server.asgi_app:
    # StreamableHTTPSessionManager.handle_request asserts its background task group
    # is already active. A separate on_event handler (rather than folding into
    # startup() above) since this one needs to be async and the other doesn't.
    await mcp_server.start()
    log.info("MCP server started")


@app.on_event("shutdown")
def shutdown() -> None:
    outbox.stop_worker()
    _scheduler.shutdown(wait=False)
    log.info("APScheduler stopped")


@app.on_event("shutdown")
async def shutdown_mcp() -> None:
    await mcp_server.stop()
    log.info("MCP server stopped")


# ── Auth ───────────────────────────────────────────────────────────────────────
#
# Issue #82 — session storage. The signed cookie (SessionMiddleware) now carries
# only an opaque `sid` token; app/sessions.py is the only place that resolves that
# token back to a user_id, against the server-side `sessions` table. Helpers below
# wrap that for the rest of this file: _client_ip (best-effort, cosmetic only),
# _start_session (the low-level "create a session row + point the cookie at it"
# primitive), _end_session (call on logout), and _set_authenticated_session (issue
# #83 — the actual call site every login/register/OAuth-callback route uses; a thin
# wrapper around _start_session that also clears any leftover 2FA-pending state,
# see its own docstring).


def _client_ip(request: Request) -> str | None:
    """Best-effort client IP for the Settings "active sessions" display only — never
    used for any access-control decision. Prefers X-Forwarded-For's first hop (set
    by the Cloudflare tunnel this instance normally sits behind) over the raw socket
    peer, which would otherwise just be the tunnel/proxy itself."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _start_session(request: Request, user_id: int) -> None:
    """Create a server-side session row for user_id and point this request's signed
    cookie at it via an opaque token. Low-level primitive — every route should call
    _set_authenticated_session (below) instead, so a 2FA-pending flow can never leak
    into a session established some other way."""
    token = sessions.create_session(user_id, request.headers.get("user-agent"), _client_ip(request))
    request.session["sid"] = token


def _end_session(request: Request) -> None:
    """Revoke this request's server-side session row (if any) and clear its cookie.
    Call this instead of request.session.clear() directly from logout — a bare
    cookie clear alone would leave the sessions row itself still active until it
    naturally expires."""
    token = request.session.get("sid")
    if token:
        sessions.revoke_session_by_token(token)
    request.session.clear()


def _set_authenticated_session(request: Request, user_id: int) -> None:
    """Grants a real logged-in session for user_id — the single place every login
    path (direct local login, the 2FA second-factor step, registration, and the
    OAuth callback) should call, instead of _start_session directly.

    Security-review finding (post-#83, re-verified after reconciling with #82's
    session-store rewrite): auth_login_submit sets session[pending_2fa_user_id] = X
    after a correct password on a 2FA-enabled account X, before the user has
    completed the second factor. Nothing previously cleared that key on any OTHER
    path that could go on to establish a session — so a user could enter X's correct
    password, abandon the /auth/login/2fa prompt, and then separately log into (or
    register, or OAuth-log into) a different account Y in the same browser session.
    The session would correctly end up pointed at Y's new server-side session row at
    that point, but the stale pending_2fa_user_id=X would still be sitting in the
    cookie alongside it; revisiting /auth/login/2fa later with a valid code for X
    would silently switch the authenticated identity back to X with no re-auth of Y
    involved. Routing every session grant through here — and through _start_session,
    the only thing that actually writes a new "sid" — closes that off
    unconditionally, including the direct (non-2FA) login path where it's merely
    defensive today.

    Under #82's session-token model this delegates to _start_session for the actual
    session-row creation/cookie write, rather than writing request.session["user_id"]
    directly the way it did before #82 — see get_current_user's own #83-era note
    about why request.session.clear() also needs to preserve pending_2fa_user_id,
    the other half of this same reconciliation."""
    request.session.pop(_PENDING_2FA_SESSION_KEY, None)
    _start_session(request, user_id)


def _resolve_session_user(token: str | None) -> tuple[dict | None, dict | None]:
    """Resolve one session token to (user_row, impersonation_context), or
    (None, None) if the token is missing/unknown/revoked/expired. Shared by
    get_current_user's primary lookup and its impersonation-expiry fallback
    below (issue #230) — split out so both call sites go through the exact same
    is_active check rather than risking the two drifting apart.

    impersonation_context, when not None, is
    {"admin_id": int, "admin_email": str | None, "expires_at": datetime} — see
    app/sessions.py's get_impersonation_context()."""
    if not token:
        return None, None
    user_id = sessions.resolve_session(token)
    if not user_id:
        return None, None
    user = db.fetchone("SELECT * FROM users WHERE id = %s", (user_id,))
    if not user:
        return None, None
    if not user["is_active"]:
        sessions.revoke_session_by_token(token)
        return None, None

    impersonation = None
    imp = sessions.get_impersonation_context(token)
    if imp:
        admin_row = db.fetchone("SELECT id, email FROM users WHERE id = %s", (imp["admin_user_id"],))
        impersonation = {
            "admin_id": imp["admin_user_id"],
            "admin_email": admin_row["email"] if admin_row else None,
            "expires_at": imp["expires_at"],
        }
    return user, impersonation


def get_current_user(request: Request) -> dict | None:
    """Return the logged-in user's row, or None if no valid session.

    A deactivated user (#85) is treated as logged out from here on — this is the
    single choke point _require_user/_require_user_api/_require_admin and the nav
    context processor all go through, so clearing the session here rejects an
    already-established session on its very next request, not just at login.

    Issue #82: resolves request.session["sid"] against the server-side sessions
    table rather than trusting a user_id straight out of the cookie. Cached on
    request.state for the duration of one request — _nav_context and whichever
    route handler is running both call this, and without the cache that's two
    identical DB round trips (users + sessions) per page load instead of one.
    Also caches the resolved impersonation context (if any) on request.state, for
    _get_current_impersonation()/the nav banner and the impersonation audit
    middleware to read without a second lookup.

    Rollout note: every pre-#82 cookie has no "sid" key at all (old shape was just
    `{"user_id": N}`), so `request.session.get("sid")` on one of those is always
    None — those requests fall straight through to the "not authenticated" branch
    below and the stale cookie gets cleared. That's a deliberate one-time re-login
    for every session that existed before this shipped, not a bug — see this
    feature's PR description for the full rollout writeup.

    Issue #83 reconciliation note: a request mid-way through the TOTP second-factor
    flow ALSO has no "sid" key yet (that's the whole point — it isn't authenticated
    until the code is verified), so it hits this same "not authenticated" branch on
    every single page render in between (every templates.TemplateResponse call runs
    the _nav_context processor, which calls this function). Without special-casing
    it, the unconditional request.session.clear() below would wipe out
    pending_2fa_user_id the moment auth_login_2fa_page renders its own response —
    before the user ever gets to submit a code — since SessionMiddleware writes
    whatever's left in request.session back to the cookie at the end of the request
    regardless of which code path touched it. pending_2fa_user_id is deliberately
    preserved across the clear; everything else about a stale/invalid session still
    gets scrubbed exactly as before.

    Issue #230 reconciliation note: while impersonating, request.session["sid"]
    points at the impersonation session row (belonging to the target user) and
    request.session["impersonator_sid"] holds the impersonating admin's own,
    separate session token — set by admin_start_impersonation, restored by
    admin_stop_impersonation. If the primary sid fails to resolve here (either it
    was just explicitly revoked by Stop, or resolve_session() above auto-revoked
    it because its own impersonation_expires_at passed) AND an impersonator_sid is
    present, this falls back to resolving THAT token and, if it's still live,
    transparently restores the admin's own session rather than just logging
    everyone out — "no risk of getting stuck in the impersonated state" (#230's
    acceptance criteria) applies to silent expiry, not only the explicit Stop
    button. If the admin's own session has also gone dead in the meantime (e.g.
    they logged out elsewhere), this falls through to the normal "not
    authenticated" branch below like any other dead session.
    """
    if hasattr(request.state, "_cached_user"):
        return request.state._cached_user

    token = request.session.get("sid")
    user, impersonation = _resolve_session_user(token)

    if user is None and token and request.session.get("impersonator_sid"):
        fallback_token = request.session.pop("impersonator_sid")
        user, impersonation = _resolve_session_user(fallback_token)
        if user is not None:
            request.session["sid"] = fallback_token
        else:
            request.session.pop("sid", None)

    if user is None:
        pending_2fa = request.session.get(_PENDING_2FA_SESSION_KEY)
        request.session.clear()
        if pending_2fa is not None:
            request.session[_PENDING_2FA_SESSION_KEY] = pending_2fa

    request.state._cached_user = user
    request.state._cached_impersonation = impersonation
    return user


def _get_current_impersonation(request: Request) -> dict | None:
    """The {"admin_id", "admin_email", "expires_at"} context cached by
    get_current_user()'s most recent call this request, or None if the current
    session isn't (or is no longer) an impersonation session. Must be called
    after get_current_user() (directly, or via _require_user/_require_admin/the
    nav context processor) has already run once this request — same
    request.state cache dependency _nav_context already relies on for nav_user."""
    if not hasattr(request.state, "_cached_user"):
        get_current_user(request)
    return getattr(request.state, "_cached_impersonation", None)


def _build_whats_new_digest(user_id: int, since: datetime) -> dict | None:
    """Issue #235 — the actual "what's new since your last visit" content, computed
    for everything that happened for this user strictly after `since`. Deliberately
    passive/summary-shaped, not a re-delivery of anything the per-event dispatcher
    (#51, app/notify.py) already pushed: new episodes aired for Watching/Planning
    shows, new (non-dismissed, non-snoozed) recommendations, and recent sync
    activity. Returns None when there's genuinely nothing new to show, so callers
    never render an empty banner.

    Episodes-aired data source, deliberately NOT airing_schedule_cache: that table
    only ever holds *not-yet-aired* rows for RELEASING anime — its AniList query
    uses notYetAired: true, and scripts/sync_airing_schedule.py deletes+reinserts
    it on every hourly refresh, so a row with airing_at in the past only exists in
    the brief window before the next refresh cleans it up (same quirk
    _compute_streaming_calendar's own docstring documents). Querying it here with
    `airing_at <= now()` would return real rows almost never in production, even
    though the digest content is exactly right conceptually.

    Instead this joins notified_episodes — the table the existing hourly job,
    _check_airing_episodes, already populates (unconditionally, independent of
    whether notify() actually had a channel configured) every time it detects a
    newly-aired episode for a WATCHING/PLANNING library entry. It's reliable,
    per-user, timestamped, and — unlike airing_schedule_cache — never deleted, so
    it's the right source for "what aired since I was last here." Status scope
    matches _check_airing_episodes' own filter (WATCHING, PLANNING) rather than
    reusing library.html's WATCHING/REPEATING tab grouping: notified_episodes is
    only ever written for those two statuses in the first place (a REPEATING-status
    filter here would just silently match nothing, since the hourly job never
    inserts a row for one), and PLANNING belongs in scope too — #256/#257's
    Upcoming view already treats PLANNING as "episodes airing that this user cares
    about," not just WATCHING.
    """
    episodes = db.fetchall(
        """
        SELECT a.id AS anime_id, a.title_english, a.title_romaji,
               COUNT(*) AS episodes_aired, MAX(ne.episode) AS latest_episode
        FROM notified_episodes ne
        JOIN library_entries le ON le.anime_id = ne.anime_id AND le.user_id = ne.user_id
        JOIN anime a ON a.id = ne.anime_id
        WHERE ne.user_id = %s
          AND ne.notified_at > %s
          AND le.status IN ('WATCHING', 'PLANNING')
        GROUP BY a.id, a.title_english, a.title_romaji
        ORDER BY MAX(ne.notified_at) DESC
        """,
        (user_id, since),
    )

    rec_rows = db.fetchall(
        """
        SELECT a.title_english, a.title_romaji, rs.score
        FROM recommendation_scores rs
        JOIN anime a ON a.id = rs.anime_id
        WHERE rs.user_id = %s
          AND rs.first_shown_at > %s
          AND rs.dismissed = false
          AND (rs.snoozed_until IS NULL OR rs.snoozed_until <= now())
        ORDER BY rs.score DESC
        """,
        (user_id, since),
    )

    sync_rows = db.fetchall(
        """
        SELECT status, entries_updated
        FROM sync_log
        WHERE user_id = %s
          AND run_at > %s
          AND type IN ('full_sync', 'force_full_resync')
        """,
        (user_id, since),
    )

    if not episodes and not rec_rows and not sync_rows:
        return None

    sync_runs = len(sync_rows)
    sync_entries_updated = sum(
        r["entries_updated"] or 0 for r in sync_rows if r["status"] == "ok"
    )
    sync_had_error = any(r["status"] in ("error", "partial") for r in sync_rows)

    return {
        "episodes": [
            {
                "anime_id": r["anime_id"],
                "title": r["title_english"] or r["title_romaji"],
                "episodes_aired": r["episodes_aired"],
                "latest_episode": r["latest_episode"],
            }
            for r in episodes
        ],
        "recommendations_count": len(rec_rows),
        "recommendation_titles": [
            r["title_english"] or r["title_romaji"] for r in rec_rows[:3]
        ],
        "sync_runs": sync_runs,
        "sync_entries_updated": sync_entries_updated,
        "sync_had_error": sync_had_error,
    }


def _whats_new_for_request(request: Request, user: dict | None, impersonation: dict | None) -> dict | None:
    """Session-gated wrapper around _build_whats_new_digest — shows the digest at
    most once per browser session (the "on login, or first page load after some
    elapsed time" trigger from #235: a fresh session covers both a real login and
    the cookie having expired/been reissued after a while away), then advances
    users.digest_last_seen_at so a second page view in the same session — or a
    login again later the same day — doesn't re-show the same items.

    Skipped entirely while impersonating: nav_user is the *target* user in that
    case, and advancing their digest watermark as a side effect of an admin
    browsing on their behalf would silently eat the real digest the target user
    would otherwise have seen next time they log in themselves.

    Deliberately called only from the library() route (`GET /` — the redirect
    target of a real login, and the natural "first page" of a return visit), not
    wired into the shared _nav_context template-context processor that runs on
    every TemplateResponse across the whole app: plenty of other routes/tests
    monkeypatch db.fetchall narrowly for their own unrelated query shape (queue
    cards, settings tabs, etc.), and a digest query firing unconditionally on
    every page would either hit those canned fixtures with the wrong shape or a
    real query most of those tests never provision for. Confining this to one
    deliberate call site keeps the "since last visit" semantics intact (nothing
    else redirects here on login) without turning every page in the app into an
    implicit dependency of this feature.
    """
    if user is None or impersonation is not None:
        return None
    if request.session.get("whats_new_shown"):
        return None
    request.session["whats_new_shown"] = True

    row = db.fetchone("SELECT digest_last_seen_at FROM users WHERE id = %s", (user["id"],))
    last_seen = row["digest_last_seen_at"] if row else None

    digest = None
    if last_seen is not None:
        digest = _build_whats_new_digest(user["id"], last_seen)

    db.execute("UPDATE users SET digest_last_seen_at = now() WHERE id = %s", (user["id"],))
    return digest


def _nav_context(request: Request) -> dict:
    """Context processor: makes the logged-in user (nav_user), the active-locale
    translator (t), and the theme preference (nav_theme) available to every
    template without each route needing to pass them explicitly. Combined into one
    processor so all three share the single get_current_user DB lookup rather than
    each doing its own.

    Locale resolution: the user's saved `language` setting wins when logged in;
    logged-out pages (auth_login.html etc, which have no settings row to read yet)
    fall back to the browser's Accept-Language header, then English. See app/i18n.py.
    Theme resolution: the user's saved `theme` setting wins when logged in; logged-out
    pages fall back to "system" (no explicit choice, so base.html sets no data-theme
    attribute and prefers-color-scheme alone decides light vs dark)."""
    user = get_current_user(request)
    impersonation = _get_current_impersonation(request)
    user_language = config.get(user["id"], "language") if user else None
    locale = i18n.resolve_locale(request.headers.get("accept-language"), user_language)
    theme = config.get(user["id"], "theme") if user else "system"
    # `<` escaped so a translated string can never accidentally close the <script>
    # tag it's embedded in (see base.html's window.I18N assignment, #147).
    i18n_json = json.dumps(i18n.all_strings(locale), ensure_ascii=False).replace("<", "\\u003c")
    return {
        "nav_user": user,
        # Issue #230 — non-None only while the current session is an admin
        # impersonating this user; drives base.html's persistent banner.
        "nav_impersonation": impersonation,
        "t": i18n.translator(locale),
        "current_language": locale,
        "nav_theme": theme,
        "i18n_json": i18n_json,
    }


templates.context_processors.append(_nav_context)


def _instance_config_get(key: str) -> str:
    row = db.fetchone("SELECT value FROM instance_config WHERE key = %s", (key,))
    return row["value"] if row else ""


def _instance_config_set(key: str, value: str) -> None:
    db.execute(
        "INSERT INTO instance_config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )


def _oauth_config(provider: str) -> tuple[str, str]:
    """(client_id, client_secret) for a provider — instance_config first, env var fallback."""
    client_id = _instance_config_get(f"{provider}_client_id") or os.getenv(f"{provider.upper()}_CLIENT_ID", "")
    client_secret = _instance_config_get(f"{provider}_client_secret") or os.getenv(f"{provider.upper()}_CLIENT_SECRET", "")
    return client_id, client_secret


def oauth_configured(provider: str) -> bool:
    client_id, client_secret = _oauth_config(provider)
    return bool(client_id and client_secret)


def _ensure_oauth_registered(provider: str) -> None:
    """(Re-)register an OAuth client with the current credentials.

    authlib's OAuth registry caches created clients by name (see create_client() in
    authlib/integrations/base_client/registry.py) — calling register() again after a
    client has already been created does NOT pick up new config, since create_client()
    short-circuits on the cache before ever looking at the updated registry entry.
    There's no public API to clear just one cached client, so we drop it directly.
    """
    oauth._clients.pop(provider, None)
    client_id, client_secret = _oauth_config(provider)
    if provider == "google":
        oauth.register(
            name="google",
            client_id=client_id,
            client_secret=client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    else:  # discord
        oauth.register(
            name="discord",
            client_id=client_id,
            client_secret=client_secret,
            access_token_url="https://discord.com/api/oauth2/token",
            authorize_url="https://discord.com/api/oauth2/authorize",
            api_base_url="https://discord.com/api/",
            client_kwargs={"scope": "identify email"},
        )


def _find_user_by_provider_identity(provider: str, provider_id: str) -> dict | None:
    """Look up a user by their linked identity for this provider.

    OAuth providers are checked via their dedicated google_id/discord_id column
    rather than auth_provider/auth_provider_id — that column is populated the same
    way whether the identity was the account's original signup method or was added
    later via /settings/link/{provider}, so linked-later accounts log in identically
    to ones that started there. 'local' is unchanged: auth_provider_id IS the
    lowercased email for local accounts.
    """
    if provider == "google":
        return db.fetchone("SELECT * FROM users WHERE google_id = %s", (provider_id,))
    if provider == "discord":
        return db.fetchone("SELECT * FROM users WHERE discord_id = %s", (provider_id,))
    return db.fetchone(
        "SELECT * FROM users WHERE auth_provider = 'local' AND auth_provider_id = %s",
        (provider_id,),
    )


def _resolve_or_create_user(
    provider: str, provider_id: str, email: str,
    display_name: str | None, avatar_url: str | None,
):
    """Look up or create a user for any auth method (local or OAuth).

    If no existing identity matches but this email already belongs to a DIFFERENT
    account, we deliberately do not merge or create a second account — that would
    mean trusting a bare email match as proof of identity, which an unverified or
    spoofed provider email could exploit. Instead the caller is told to log in with
    whatever method they already have and link this one from Settings
    (/settings/link/{provider}), which only ever runs for an already-authenticated
    session — see auth_link_callback.

    Returns (user_dict, None) on success, or (None, rejection_response) if this email
    isn't allowed to create an account, or already belongs to someone else — shared by
    the OAuth callback and local registration so an invite means the same thing
    regardless of login method.
    """
    user = _find_user_by_provider_identity(provider, provider_id)
    if user:
        if not user["is_active"]:
            return None, HTMLResponse(
                "<h1>Account deactivated</h1><p>This account has been deactivated. "
                "Contact your admin if you think this is a mistake.</p>",
                status_code=403,
            )
        db.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user["id"],))
        return user, None

    existing_by_email = db.fetchone("SELECT * FROM users WHERE email = %s", (email,))
    if existing_by_email:
        return None, HTMLResponse(
            "<h1>Account already exists</h1>"
            f"<p>{html.escape(email)} already has an account here via a different login "
            "method. Log in that way, then connect this one from Settings.</p>",
            status_code=409,
        )

    user_count = db.fetchone("SELECT COUNT(*) AS n FROM users")["n"]
    is_admin = user_count == 0

    invite = None
    if not is_admin:
        # Issue #231 — an invite row can exist but no longer be usable: already
        # revoked by an admin, or past its expires_at window. Either case must be
        # rejected exactly like "no invite at all", not silently accepted just
        # because a row with this email happens to still be sitting in the table.
        invite = db.fetchone(
            "SELECT * FROM invites WHERE email = %s AND accepted_at IS NULL "
            "AND revoked_at IS NULL AND expires_at > now()",
            (email,),
        )
        if not invite:
            return None, HTMLResponse(
                "<h1>Not invited</h1>"
                f"<p>{html.escape(email)} hasn't been invited to this instance, or "
                "their invite has expired or been revoked. Ask the admin to "
                "(re)invite you.</p>",
                status_code=403,
            )

    google_id = provider_id if provider == "google" else None
    discord_id = provider_id if provider == "discord" else None

    user = db.execute_returning(
        """
        INSERT INTO users (
            auth_provider, auth_provider_id, email, google_id, discord_id,
            display_name, avatar_url, is_admin, last_login_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        RETURNING *
        """,
        (provider, provider_id, email, google_id, discord_id, display_name, avatar_url, is_admin),
    )
    if invite:
        db.execute(
            "UPDATE invites SET accepted_at = now(), accepted_by = %s WHERE id = %s",
            (user["id"], invite["id"]),
        )
    return user, None


def _invite_status(invite: dict, now: datetime) -> str:
    """Issue #231 — the real state of an invite row for Admin -> Invites display,
    computed rather than stored: accepted always wins (once used, expiry/revocation
    are moot), then revoked, then expired (expires_at in the past), else pending.
    Order matters — an accepted invite can still have an expires_at in the past
    (it was used before it expired) and must still read as "accepted", not "expired"."""
    if invite["accepted_at"]:
        return "accepted"
    if invite["revoked_at"]:
        return "revoked"
    if invite["expires_at"] and invite["expires_at"] < now:
        return "expired"
    return "pending"


def _no_users_exist() -> bool:
    """True on a fresh install (empty users table) — same condition
    _resolve_or_create_user already checks to decide bootstrap-admin, surfaced here
    too so an unauthenticated visit lands on registration instead of a login form
    with no one to log in as yet."""
    return db.fetchone("SELECT COUNT(*) AS n FROM users")["n"] == 0


@app.get("/auth/login", response_class=HTMLResponse)
def auth_login_page(request: Request, error: str = ""):
    if _no_users_exist():
        return RedirectResponse(url="/auth/register", status_code=303)
    return templates.TemplateResponse(
        request,
        "auth_login.html",
        {
            "error": error,
            "oauth_google_configured": oauth_configured("google"),
            "oauth_discord_configured": oauth_configured("discord"),
        },
    )


@app.post("/auth/login")
def auth_login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    # Match by email regardless of auth_provider — an OAuth-originated account can
    # have a local password set from Settings too (see #11), and auth_provider is
    # purely historical ("how this account was originally created"), not a gate on
    # which login methods currently work. password_hash being unset (OAuth-only,
    # never added a password) still correctly fails the check below.
    user = db.fetchone("SELECT * FROM users WHERE email = %s", (email,))

    if user and user["locked_until"] and user["locked_until"] > datetime.now(timezone.utc):
        minutes_left = max(1, int((user["locked_until"] - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
        return RedirectResponse(
            url=f"/auth/login?error=Too+many+failed+attempts.+Try+again+in+{minutes_left}+minutes",
            status_code=303,
        )

    valid = user and user["password_hash"] and bcrypt.checkpw(
        password.encode("utf-8"), user["password_hash"].encode("utf-8")
    )
    if not valid:
        if user:
            attempts = user["failed_login_attempts"] + 1
            if attempts >= _LOGIN_MAX_ATTEMPTS:
                db.execute(
                    "UPDATE users SET failed_login_attempts = %s, locked_until = now() + (%s * interval '1 minute') WHERE id = %s",
                    (attempts, _LOGIN_LOCKOUT_MINUTES, user["id"]),
                )
            else:
                db.execute(
                    "UPDATE users SET failed_login_attempts = %s WHERE id = %s",
                    (attempts, user["id"]),
                )
        return RedirectResponse(
            url="/auth/login?error=Invalid+email+or+password", status_code=303
        )

    if not user["is_active"]:
        return RedirectResponse(
            url="/auth/login?error=This+account+has+been+deactivated", status_code=303
        )

    if user["totp_enabled"]:
        # Issue #83 — hold off on last_login_at/the real session until the second
        # factor also succeeds. The password itself is proven correct at this point,
        # so failed_login_attempts/locked_until (the PASSWORD guess budget) reset
        # right away, same as the no-2FA path below — that's a separate concern from
        # totp_failed_attempts/totp_locked_until (the CODE guess budget), which
        # auth_login_2fa_submit owns exclusively from here on. If that TOTP counter
        # is already locked, say so now instead of bouncing the user into a 2FA
        # prompt they can't actually get past.
        if user["totp_locked_until"] and user["totp_locked_until"] > datetime.now(timezone.utc):
            minutes_left = max(1, int((user["totp_locked_until"] - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
            return RedirectResponse(
                url=f"/auth/login?error=Too+many+failed+attempts.+Try+again+in+{minutes_left}+minutes",
                status_code=303,
            )
        db.execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL WHERE id = %s",
            (user["id"],),
        )
        request.session[_PENDING_2FA_SESSION_KEY] = user["id"]
        return RedirectResponse(url="/auth/login/2fa", status_code=303)

    db.execute(
        "UPDATE users SET last_login_at = now(), failed_login_attempts = 0, locked_until = NULL WHERE id = %s",
        (user["id"],),
    )
    _set_authenticated_session(request, user["id"])
    return RedirectResponse(url="/", status_code=303)


@app.get("/auth/login/2fa", response_class=HTMLResponse)
def auth_login_2fa_page(request: Request, error: str = ""):
    """Second-factor prompt (issue #83) — only reachable via the pending_2fa_user_id
    session key auth_login_submit sets after a correct password on an account with
    TOTP enabled. Never starts a real session itself; that only happens on a
    verified code/recovery code below, via _set_authenticated_session (issue #82's
    session-token model under the hood, see that helper's docstring)."""
    if not request.session.get(_PENDING_2FA_SESSION_KEY):
        return RedirectResponse(url="/auth/login", status_code=303)
    return templates.TemplateResponse(request, "auth_login_2fa.html", {"error": error})


@app.post("/auth/login/2fa")
def auth_login_2fa_submit(request: Request, code: str = Form(...)):
    pending_id = request.session.get(_PENDING_2FA_SESSION_KEY)
    if not pending_id:
        return RedirectResponse(url="/auth/login", status_code=303)

    user = db.fetchone("SELECT * FROM users WHERE id = %s", (pending_id,))
    if not user or not user["totp_enabled"] or not user["is_active"]:
        # Account state changed mid-flow (2FA disabled elsewhere, deactivated, or
        # deleted) — no valid pending login to complete.
        request.session.pop(_PENDING_2FA_SESSION_KEY, None)
        return RedirectResponse(url="/auth/login", status_code=303)

    if user["totp_locked_until"] and user["totp_locked_until"] > datetime.now(timezone.utc):
        request.session.pop(_PENDING_2FA_SESSION_KEY, None)
        minutes_left = max(1, int((user["totp_locked_until"] - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
        return RedirectResponse(
            url=f"/auth/login?error=Too+many+failed+attempts.+Try+again+in+{minutes_left}+minutes",
            status_code=303,
        )

    code = code.strip()
    valid = bool(code) and pyotp.TOTP(config.decrypt_secret(user["totp_secret"])).verify(code, valid_window=1)
    if not valid and code:
        # Falls back to a one-time recovery code (issue #83's lost-authenticator path)
        # — only tried if the TOTP check itself failed, so a valid 6-digit code is
        # never wasted second-guessing it against the recovery-code table.
        valid = _consume_recovery_code_if_valid(user["id"], code) is not None

    if not valid:
        attempts = user["totp_failed_attempts"] + 1
        if attempts >= _TOTP_LOGIN_MAX_ATTEMPTS:
            db.execute(
                "UPDATE users SET totp_failed_attempts = %s, totp_locked_until = now() + (%s * interval '1 minute') WHERE id = %s",
                (attempts, _TOTP_LOGIN_LOCKOUT_MINUTES, user["id"]),
            )
            # Crossing the lockout threshold ends this login attempt just as
            # definitively as the two early-return branches above — clear the
            # pending state AND redirect straight to /auth/login with the lockout
            # message immediately, rather than back to /auth/login/2fa, so an
            # immediate follow-up GET can't re-render a code-entry form the account
            # is now locked out of even for one extra round trip.
            request.session.pop(_PENDING_2FA_SESSION_KEY, None)
            return RedirectResponse(
                url=f"/auth/login?error=Too+many+failed+attempts.+Try+again+in+{_TOTP_LOGIN_LOCKOUT_MINUTES}+minutes",
                status_code=303,
            )
        db.execute(
            "UPDATE users SET totp_failed_attempts = %s WHERE id = %s",
            (attempts, user["id"]),
        )
        return RedirectResponse(url="/auth/login/2fa?error=Invalid+code", status_code=303)

    db.execute(
        "UPDATE users SET last_login_at = now(), totp_failed_attempts = 0, totp_locked_until = NULL WHERE id = %s",
        (user["id"],),
    )
    _set_authenticated_session(request, user["id"])
    return RedirectResponse(url="/", status_code=303)


@app.get("/auth/register", response_class=HTMLResponse)
def auth_register_page(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "auth_register.html", {"error": error})


@app.post("/auth/register")
def auth_register_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    if len(password) < 8:
        return RedirectResponse(
            url="/auth/register?error=Password+must+be+at+least+8+characters", status_code=303
        )

    existing = db.fetchone(
        "SELECT id FROM users WHERE auth_provider = 'local' AND auth_provider_id = %s",
        (email,),
    )
    if existing:
        return RedirectResponse(
            url="/auth/register?error=An+account+with+that+email+already+exists", status_code=303
        )

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    user, denied = _resolve_or_create_user("local", email, email, None, None)
    if denied:
        return denied
    db.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user["id"]))

    _set_authenticated_session(request, user["id"])
    return RedirectResponse(url="/", status_code=303)


def _valid_reset_token(token: str):
    """Returns the password_resets row if the token is usable, else None."""
    row = db.fetchone(
        "SELECT * FROM password_resets WHERE token = %s AND used_at IS NULL AND expires_at > now()",
        (token,),
    )
    return row


@app.get("/auth/reset-password/{token}", response_class=HTMLResponse)
def auth_reset_password_page(request: Request, token: str, error: str = ""):
    valid = bool(_valid_reset_token(token))
    return templates.TemplateResponse(
        request,
        "auth_reset_password.html",
        {"valid": valid, "token": token, "error": error},
        status_code=200 if valid else 400,
    )


@app.post("/auth/reset-password/{token}")
def auth_reset_password_submit(request: Request, token: str, password: str = Form(...)):
    reset = _valid_reset_token(token)
    if not reset:
        return templates.TemplateResponse(
            request,
            "auth_reset_password.html",
            {"valid": False, "token": token, "error": ""},
            status_code=400,
        )
    if len(password) < 8:
        return RedirectResponse(
            url=f"/auth/reset-password/{token}?error=Password+must+be+at+least+8+characters",
            status_code=303,
        )

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    db.execute(
        "UPDATE users SET password_hash = %s, failed_login_attempts = 0, locked_until = NULL WHERE id = %s",
        (password_hash, reset["user_id"]),
    )
    db.execute("UPDATE password_resets SET used_at = now() WHERE token = %s", (token,))

    return RedirectResponse(url="/auth/login", status_code=303)


@app.get("/auth/login/{provider}")
async def auth_login(request: Request, provider: str):
    if provider not in AUTH_PROVIDERS:
        return JSONResponse({"error": "unknown provider"}, status_code=404)
    if not oauth_configured(provider):
        return HTMLResponse(
            f"<h1>Not configured</h1><p>{provider.title()} login isn't set up on this "
            "instance yet. An admin can configure it under Admin → OAuth settings.</p>",
            status_code=404,
        )
    _ensure_oauth_registered(provider)
    client = oauth.create_client(provider)
    redirect_uri = request.url_for("auth_callback", provider=provider)
    return await client.authorize_redirect(request, redirect_uri)


async def _fetch_oauth_profile(client, provider: str, token: dict) -> tuple[str, str, str | None, str | None]:
    """Returns (provider_user_id, email, display_name, avatar_url) from the provider's
    profile. Shared by the login callback and the link callback — same profile shape
    either way, only what's done with it afterward differs."""
    if provider == "google":
        profile = token.get("userinfo") or await client.userinfo(token=token)
        provider_user_id = str(profile["sub"])
        email = profile.get("email", "")
        display_name = profile.get("name")
        avatar_url = profile.get("picture")
    else:  # discord
        resp = await client.get("users/@me", token=token)
        profile = resp.json()
        provider_user_id = str(profile["id"])
        email = profile.get("email", "")
        display_name = profile.get("username")
        avatar_hash = profile.get("avatar")
        avatar_url = (
            f"https://cdn.discordapp.com/avatars/{provider_user_id}/{avatar_hash}.png"
            if avatar_hash else None
        )
    # Normalize the same way every other auth path already does (local login/register,
    # admin invite creation) — otherwise an invite for "friend@example.com" can be
    # incorrectly rejected if the provider hands back different casing.
    return provider_user_id, email.strip().lower(), display_name, avatar_url


@app.get("/auth/callback/{provider}")
async def auth_callback(request: Request, provider: str):
    if provider not in AUTH_PROVIDERS:
        return JSONResponse({"error": "unknown provider"}, status_code=404)
    _ensure_oauth_registered(provider)
    client = oauth.create_client(provider)
    token = await client.authorize_access_token(request)

    provider_user_id, email, display_name, avatar_url = await _fetch_oauth_profile(client, provider, token)

    user, denied = _resolve_or_create_user(provider, provider_user_id, email, display_name, avatar_url)
    if denied:
        return denied

    _set_authenticated_session(request, user["id"])
    return RedirectResponse(url="/")


@app.get("/settings/link/{provider}")
async def settings_link(request: Request, provider: str):
    """Start an OAuth round trip to connect provider to the CURRENT logged-in account.

    Deliberately a separate entry point from /auth/login/{provider} rather than
    reusing it based on ambient session state — linking should only ever happen
    because the user explicitly clicked "Connect" while already authenticated, not
    as an implicit side effect of an ordinary login click.
    """
    user, denied = _require_user(request)
    if denied:
        return denied
    if provider not in AUTH_PROVIDERS:
        return JSONResponse({"error": "unknown provider"}, status_code=404)
    if not oauth_configured(provider):
        return HTMLResponse(
            f"<h1>Not configured</h1><p>{provider.title()} login isn't set up on this "
            "instance yet.</p>",
            status_code=404,
        )
    _ensure_oauth_registered(provider)
    client = oauth.create_client(provider)
    redirect_uri = request.url_for("auth_link_callback", provider=provider)
    return await client.authorize_redirect(request, redirect_uri)


@app.get("/auth/link-callback/{provider}")
async def auth_link_callback(request: Request, provider: str):
    user, denied = _require_user(request)
    if denied:
        return denied
    if provider not in AUTH_PROVIDERS:
        return JSONResponse({"error": "unknown provider"}, status_code=404)

    _ensure_oauth_registered(provider)
    client = oauth.create_client(provider)
    token = await client.authorize_access_token(request)
    provider_user_id, _email, _display_name, _avatar_url = await _fetch_oauth_profile(client, provider, token)

    existing = _find_user_by_provider_identity(provider, provider_user_id)
    if existing and existing["id"] != user["id"]:
        return RedirectResponse(
            url=f"/settings?link_error=That+{provider.title()}+account+is+already+"
                "connected+to+a+different+login",
            status_code=303,
        )
    if not existing:
        if provider == "google":
            db.execute("UPDATE users SET google_id = %s WHERE id = %s", (provider_user_id, user["id"]))
        else:
            db.execute("UPDATE users SET discord_id = %s WHERE id = %s", (provider_user_id, user["id"]))

    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/unlink/{provider}")
def settings_unlink(request: Request, provider: str):
    user, denied = _require_user(request)
    if denied:
        return denied
    if provider not in ("google", "discord"):
        return JSONResponse({"error": "unknown provider"}, status_code=404)

    # Re-fetch — the dict from _require_user could be stale relative to a link/unlink
    # that happened earlier in this same session.
    current = db.fetchone("SELECT * FROM users WHERE id = %s", (user["id"],))
    other_methods_left = bool(current["password_hash"])
    if provider != "google":
        other_methods_left = other_methods_left or bool(current["google_id"])
    if provider != "discord":
        other_methods_left = other_methods_left or bool(current["discord_id"])
    if not other_methods_left:
        return RedirectResponse(
            url="/settings?link_error=Can%27t+disconnect+your+only+login+method",
            status_code=303,
        )

    if provider == "google":
        db.execute("UPDATE users SET google_id = NULL WHERE id = %s", (user["id"],))
    else:
        db.execute("UPDATE users SET discord_id = NULL WHERE id = %s", (user["id"],))

    return RedirectResponse(url="/settings", status_code=303)


@app.post("/auth/logout")
async def auth_logout(request: Request):
    _end_session(request)
    return RedirectResponse(url="/", status_code=303)


def _require_admin(request: Request):
    """Returns a Response to send back if the caller isn't an admin, else None."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)
    if not user["is_admin"]:
        return HTMLResponse("<h1>Forbidden</h1><p>Admin access required.</p>", status_code=403)
    return None


def _log_admin_action(admin_user_id: int, action: str, target_user_id: int | None = None, detail: str | None = None):
    """Issue #89 — append a row to admin_audit_log. Called after the action itself
    has already taken effect, so a logging failure never blocks the action."""
    db.execute(
        "INSERT INTO admin_audit_log (admin_user_id, action, target_user_id, detail) VALUES (%s, %s, %s, %s)",
        (admin_user_id, action, target_user_id, detail),
    )


# ── Impersonation (issue #230) ───────────────────────────────────────────────
#
# "Login as user" for support/debugging: an admin-initiated, time-boxed
# (sessions.IMPERSONATION_TTL_MINUTES) session that acts AS the target user,
# with the same privileges that user's own session would have — never more,
# never a way to bypass their own auth state (deactivation still ends it
# immediately, same is_active check every ordinary session goes through — see
# _resolve_session_user). Never password-based: the admin never sees or needs
# the target's credentials, only sessions.start_impersonation_session().
#
# Implementation shape: starting impersonation swaps request.session["sid"] to a
# brand-new session row scoped to the target user, and stashes the admin's own
# current sid under request.session["impersonator_sid"] — both inside the
# signed, tamper-evident SessionMiddleware cookie, so a client can read but
# never forge that key to point at a token it doesn't already legitimately own.
# Ending impersonation (explicitly via Stop, or automatically once
# impersonation_expires_at passes — see get_current_user's #230 note) swaps sid
# back to impersonator_sid. There is deliberately no separate "impersonation
# session" concept at the DB level beyond the two nullable columns on `sessions`
# itself (migration 027) — it's a normal session row in every other respect.
_IMPERSONATE_PATH_RE = re.compile(r"^/admin/users/\d+/impersonate$")


@app.post("/admin/users/{user_id}/impersonate")
def admin_start_impersonation(request: Request, user_id: int):
    denied = _require_admin(request)
    if denied:
        return denied

    admin_user = get_current_user(request)
    if admin_user["id"] == user_id:
        return HTMLResponse(
            "<h1>Can't impersonate your own account</h1><p><a href=\"/admin\">Back to admin</a></p>",
            status_code=400,
        )
    if request.session.get("impersonator_sid"):
        # Already impersonating someone (or a stale key survived some other
        # path) — refuse to stack a second impersonation session on top rather
        # than silently overwriting the way back to the original admin session.
        return HTMLResponse(
            "<h1>Already impersonating another user</h1>"
            "<p>End that session first.</p><p><a href=\"/admin\">Back to admin</a></p>",
            status_code=400,
        )

    target = db.fetchone("SELECT * FROM users WHERE id = %s", (user_id,))
    if not target:
        return HTMLResponse("<h1>User not found</h1>", status_code=404)
    if not target["is_active"]:
        return HTMLResponse(
            "<h1>Can't impersonate a deactivated account</h1><p><a href=\"/admin\">Back to admin</a></p>",
            status_code=400,
        )

    admin_sid = request.session.get("sid")
    if not admin_sid:
        # _require_admin above already proved a live session exists, so this
        # shouldn't happen — but never start an impersonation session with no
        # recorded way back to it.
        return RedirectResponse(url="/auth/login", status_code=303)

    new_token = sessions.start_impersonation_session(
        admin_user["id"], user_id, request.headers.get("user-agent"), _client_ip(request)
    )
    request.session["impersonator_sid"] = admin_sid
    request.session["sid"] = new_token

    _log_admin_action(
        admin_user["id"],
        "impersonation_started",
        target_user_id=user_id,
        detail=f"target_email={target['email']}; ttl_minutes={sessions.IMPERSONATION_TTL_MINUTES}",
    )
    return RedirectResponse(url="/", status_code=303)


@app.post("/admin/impersonate/stop")
def admin_stop_impersonation(request: Request):
    """Ends the current impersonation session (if any) and returns to the
    admin's own session. Deliberately NOT gated by _require_admin — by the time
    this fires, request.session["sid"] points at the *target* user's session
    row, whose is_admin may well be false, and the banner's Stop button has to
    work for that non-admin identity. The real authorization boundary is
    structural instead: this can only do anything meaningful when
    impersonator_sid is actually present, and the only place that ever sets it
    is admin_start_impersonation above, itself gated by _require_admin — a
    non-admin target hitting this route on their own ordinary session just gets
    the harmless no-op "nothing to stop" branch below."""
    target_user = get_current_user(request)
    impersonation = _get_current_impersonation(request)
    admin_sid = request.session.get("impersonator_sid")

    if not admin_sid or not impersonation:
        return RedirectResponse(url="/", status_code=303)

    current_token = request.session.get("sid")
    if current_token:
        sessions.revoke_session_by_token(current_token)

    request.session["sid"] = admin_sid
    request.session.pop("impersonator_sid", None)

    _log_admin_action(
        impersonation["admin_id"],
        "impersonation_ended",
        target_user_id=target_user["id"] if target_user else None,
        detail=f"target_email={target_user['email']}" if target_user else None,
    )
    return RedirectResponse(url="/admin", status_code=303)


@app.middleware("http")
async def _impersonation_audit_middleware(request: Request, call_next):
    """Issue #230 — best-effort audit trail for write actions taken WHILE
    impersonating, on top of the explicit start/stop logging above. Generic
    (every mutating request) rather than instrumenting each individual route,
    so a future write endpoint can't silently slip through unaudited the way an
    opt-in per-route call would.

    Reads request.session only AFTER call_next() returns, not before — the ASGI
    `scope` dict (and the session dict SessionMiddleware stores on it) is the
    same object reference threaded through every layer of the stack regardless
    of this middleware's registration order relative to SessionMiddleware, so by
    the time control returns here it reflects whatever the route handler itself
    left in request.session. This also means the request for
    admin_start_impersonation itself doesn't get double-logged as an
    "impersonation_action" here: impersonator_sid only becomes non-empty at the
    very end of that request, but _IMPERSONATE_PATH_RE excludes it explicitly
    anyway to make that independent of timing details. admin_stop_impersonation
    needs no such exclusion — by the time this runs, it has already popped
    impersonator_sid itself, so the check below naturally no-ops for it.

    Wrapped in a broad try/except, same philosophy _log_admin_action's own
    docstring states: a logging failure must never block or alter the actual
    response."""
    response = await call_next(request)
    try:
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and request.session.get("impersonator_sid")
            and not _IMPERSONATE_PATH_RE.match(request.url.path)
        ):
            token = request.session.get("sid")
            if token:
                imp = sessions.get_impersonation_context(token)
                if imp:
                    cached_user = getattr(request.state, "_cached_user", None)
                    target_user_id = cached_user["id"] if cached_user else sessions.resolve_session(token)
                    _log_admin_action(
                        imp["admin_user_id"],
                        "impersonation_action",
                        target_user_id=target_user_id,
                        detail=f"{request.method} {request.url.path} -> {response.status_code}",
                    )
    except Exception:
        log.exception("impersonation audit logging failed")
    return response


GITHUB_REPO_URL = "https://github.com/Napandee/AniDex"


def _build_version() -> str | None:
    """The short git SHA baked into the image via the Dockerfile's GIT_SHA build
    arg (see Dockerfile), if any. Shared by the admin-only Operability tab
    (_instance_health below) and the user-facing "currently deployed commit" link
    on every user's own Settings page (issue #204) — one place reads GIT_SHA,
    not two."""
    git_sha = os.environ.get("GIT_SHA", "").strip()
    return git_sha[:12] if git_sha else None


def _instance_health() -> dict:
    """Read-only instance-health data for the admin panel (issue #86): running
    build version (if baked into the image via the Dockerfile's GIT_SHA build
    arg — see Dockerfile), Postgres database size, and row counts for a few key
    tables. Display only — deliberately no write/control actions here."""
    db_size_row = db.fetchone(
        "SELECT pg_size_pretty(pg_database_size(current_database())) AS size"
    )

    return {
        "build_version": _build_version(),
        "db_size": db_size_row["size"] if db_size_row else None,
        "row_counts": {
            "library_entries": db.fetchone("SELECT COUNT(*) AS n FROM library_entries")["n"],
            "anime": db.fetchone("SELECT COUNT(*) AS n FROM anime")["n"],
            "users": db.fetchone("SELECT COUNT(*) AS n FROM users")["n"],
        },
    }


# Issue #202 — drift-detection threshold: how many days a WATCHING/REPEATING
# library_entries row's anilist_updated_at can go without moving before it's flagged
# as a drift candidate. The default full sync runs daily (see the Sync schedule
# section of the Instance Config tab), so 30 days is roughly 30x that cadence —
# comfortably longer than "hasn't watched anything for a couple of weeks" (which is
# completely normal and shouldn't page anyone), but still tight enough to surface a
# genuine multi-week sync gap well before a user notices and files a bug (the #159
# CR season-mismatch bug — progress silently written to the wrong AniList entry
# while the real one sat stalled — is exactly the shape of problem this is meant to
# catch). Deliberately a single fixed threshold, not a per-user adaptive one — see
# #202's scope note against over-engineering v1.
DATA_QUALITY_DRIFT_DAYS = 30

# Only actively-progressing statuses are checked for drift. COMPLETED/DROPPED/PAUSED/
# PLANNING rows aren't expected to see anilist_updated_at move regardless of sync
# health, so including them would just be noise.
_DRIFT_STATUSES = ("WATCHING", "REPEATING")

# sync_log.steps (full_sync/force_full_resync only — see schema.sql's column comment)
# records one entry per provider; these are the provider names that appear there.
_SYNC_PROVIDERS = ("crunchyroll", "netflix", "anilist_postgres")

# Lookback window for the failure-rate/history section — long enough to show a real
# pattern, short enough that a since-fixed problem ages out of view on its own.
DATA_QUALITY_FAILURE_WINDOW_DAYS = 30


def _data_quality_signals() -> dict:
    """Read-only aggregation for issue #202's admin Data Quality tab. Every section
    here is computed from tables/columns that already exist — sync_log,
    library_entries, recommendation_scores, personal_notes — nothing here is new
    tracking infrastructure, and nothing here writes anything back (display only,
    same contract as _instance_health() above).

    Four sections, matching #202's acceptance criteria:

    1. last_sync_by_provider: per (user_id, provider), the most recent full_sync/
       force_full_resync run at which that provider's step succeeded
       (`last_ok_at`), plus the status/timestamp of the single most recent attempt
       regardless of outcome (`last_attempt_at`/`last_attempt_status`). Providers
       are read out of sync_log.steps rather than tracked in any new column — a
       provider a user has never configured (no CR/Netflix credentials) only ever
       shows up with a 'skipped' status, never 'ok'/'error', and the template
       renders that as "not configured".

    2. failure_history: for each user, sync_log rows of type full_sync/
       force_full_resync from the last DATA_QUALITY_FAILURE_WINDOW_DAYS days,
       aggregated into a total/failed run count and failure rate, plus the
       individual failed runs (most recent 10) for drill-down.

    3. orphaned_personal_notes: personal_notes rows with no matching
       library_entries row for the same (user_id, anime_id). This should be
       impossible in steady state — personal_notes is meant to always describe an
       anime that's actually in the user's library, see every join against it
       elsewhere in this file (e.g. the /notes and /library routes) — but nothing
       at the DB level enforces it, so a library_entries row deleted out from
       under it (an AniList-side removal synced locally) would leave exactly this
       kind of orphan behind.

    4. stale_recommendations: recommendation_scores rows (dismissed = false) for
       an anime_id that NOW has a matching library_entries row. This is
       deliberately the inverse of "no matching library_entries row" — unlike
       personal_notes, recommendation_scores candidates are only ever generated
       for anime NOT already in the user's library (see
       scripts/run_recommender.py's fetch_recommendation_candidates/
       get_library_ids, which explicitly excludes every anime_id already in
       library_entries, any status, before scoring). So "no matching
       library_entries row" is true of essentially every recommendation_scores
       row by design and would be pure noise here — the actually-orphaned case is
       the opposite: a candidate the user has since acted on (added to their
       library) that score_and_store() never removes, since it only upserts on
       each weekly re-run rather than reacting to library changes in between.
       See the PR for #202 for this explicit interpretation note.
    """
    # ── 1. Last successful sync per provider ────────────────────────────────────
    sync_step_rows = db.fetchall(
        """
        SELECT user_id, run_at, steps
        FROM sync_log
        WHERE type IN ('full_sync', 'force_full_resync') AND steps IS NOT NULL
        ORDER BY user_id, run_at DESC
        """
    )
    last_sync_by_provider: dict[int, dict[str, dict]] = {}
    for row in sync_step_rows:
        per_provider = last_sync_by_provider.setdefault(row["user_id"], {})
        for step in row["steps"] or []:
            provider = step.get("service")
            if provider not in _SYNC_PROVIDERS:
                continue
            entry = per_provider.setdefault(provider, {})
            # Rows arrive newest-first per user, so the first one seen for a given
            # provider is that provider's most recent attempt.
            if "last_attempt_at" not in entry:
                entry["last_attempt_at"] = row["run_at"]
                entry["last_attempt_status"] = step.get("status")
            if step.get("status") == "ok" and "last_ok_at" not in entry:
                entry["last_ok_at"] = row["run_at"]

    # ── 2. Recent sync failure rate/history ─────────────────────────────────────
    window_start = datetime.now(timezone.utc) - timedelta(days=DATA_QUALITY_FAILURE_WINDOW_DAYS)
    recent_runs = db.fetchall(
        """
        SELECT user_id, run_at, status, error_msg, type
        FROM sync_log
        WHERE type IN ('full_sync', 'force_full_resync') AND run_at >= %s
        ORDER BY user_id, run_at DESC
        """,
        (window_start,),
    )
    failure_history: dict[int, dict] = {}
    for row in recent_runs:
        agg = failure_history.setdefault(row["user_id"], {"total": 0, "failed": 0, "failures": []})
        agg["total"] += 1
        if row["status"] == "error":
            agg["failed"] += 1
            agg["failures"].append(row)
    for agg in failure_history.values():
        agg["failure_rate"] = (agg["failed"] / agg["total"]) if agg["total"] else 0.0
        agg["failures"] = agg["failures"][:10]

    # ── 3. Orphaned personal_notes ───────────────────────────────────────────────
    orphaned_personal_notes = db.fetchall(
        """
        SELECT pn.id, pn.user_id, u.email AS user_email, pn.anime_id,
               a.title_romaji, pn.updated_at
        FROM personal_notes pn
        LEFT JOIN library_entries le ON le.user_id = pn.user_id AND le.anime_id = pn.anime_id
        LEFT JOIN users u ON u.id = pn.user_id
        LEFT JOIN anime a ON a.id = pn.anime_id
        WHERE le.id IS NULL
        ORDER BY pn.updated_at DESC
        """
    )

    # ── 4. Stale recommendation_scores (see docstring for why this is inverted) ─
    stale_recommendations = db.fetchall(
        """
        SELECT rs.id, rs.user_id, u.email AS user_email, rs.anime_id,
               a.title_romaji, rs.computed_at, le.status AS library_status
        FROM recommendation_scores rs
        JOIN library_entries le ON le.user_id = rs.user_id AND le.anime_id = rs.anime_id
        LEFT JOIN users u ON u.id = rs.user_id
        LEFT JOIN anime a ON a.id = rs.anime_id
        WHERE rs.dismissed = false
        ORDER BY rs.computed_at DESC
        """
    )

    # ── Drift candidates ─────────────────────────────────────────────────────────
    drift_threshold = datetime.now(timezone.utc) - timedelta(days=DATA_QUALITY_DRIFT_DAYS)
    drift_candidates = db.fetchall(
        """
        SELECT le.id, le.user_id, u.email AS user_email, le.anime_id,
               a.title_romaji, le.status, le.anilist_updated_at, le.synced_at
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        LEFT JOIN users u ON u.id = le.user_id
        WHERE le.status = ANY(%s)
          AND le.anilist_updated_at IS NOT NULL
          AND le.anilist_updated_at < %s
        ORDER BY le.anilist_updated_at ASC
        """,
        (list(_DRIFT_STATUSES), drift_threshold),
    )

    return {
        "providers": _SYNC_PROVIDERS,
        "last_sync_by_provider": last_sync_by_provider,
        "failure_history": failure_history,
        "failure_window_days": DATA_QUALITY_FAILURE_WINDOW_DAYS,
        "orphaned_personal_notes": orphaned_personal_notes,
        "stale_recommendations": stale_recommendations,
        "drift_candidates": drift_candidates,
        "drift_threshold_days": DATA_QUALITY_DRIFT_DAYS,
    }


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, saved: str = ""):
    denied = _require_admin(request)
    if denied:
        return denied

    instance_health = _instance_health()
    data_quality = _data_quality_signals()

    # Sync schedule (issue #96) — moved here from Settings since it's instance-wide,
    # not per-user. Same instance_config fields _apply_schedule() reads.
    schedule = {
        "sync_daily_time": _instance_config_get("sync_daily_time") or "04:30",
        "sync_recommender_day": _instance_config_get("sync_recommender_day") or "sun",
        "sync_recommender_time": _instance_config_get("sync_recommender_time") or "05:00",
    }

    now = datetime.now(timezone.utc)

    invites = db.fetchall("SELECT * FROM invites ORDER BY created_at DESC")
    invites_view = [{**i, "status": _invite_status(i, now)} for i in invites]

    users = db.fetchall("SELECT * FROM users ORDER BY created_at DESC")

    # Most recent full_sync row per user — operability visibility for the admin
    # running the instance, not just invite/user management.
    sync_rows = db.fetchall(
        """
        SELECT DISTINCT ON (user_id) user_id, run_at, status, error_msg
        FROM sync_log
        WHERE type = 'full_sync'
        ORDER BY user_id, run_at DESC
        """
    )
    last_sync_by_user = {r["user_id"]: r for r in sync_rows}

    # Issue #84 — same operability visibility for the recommender job that sync
    # already has above.
    recommender_rows = db.fetchall(
        """
        SELECT DISTINCT ON (user_id) user_id, run_at, status, error_msg
        FROM sync_log
        WHERE type = 'recommender'
        ORDER BY user_id, run_at DESC
        """
    )
    last_recommender_by_user = {r["user_id"]: r for r in recommender_rows}

    users_view = []
    for u in users:
        users_view.append({
            **u,
            "locked": bool(u["locked_until"] and u["locked_until"] > now),
            "last_sync": last_sync_by_user.get(u["id"]),
            "last_recommender": last_recommender_by_user.get(u["id"]),
        })

    def _provider_status(provider: str) -> dict:
        client_id, client_secret = _oauth_config(provider)
        return {"configured": bool(client_id and client_secret), "client_id": client_id}

    default_hidden_tags = json.loads(_instance_config_get("default_hidden_tags") or "[]")

    # Issue #89 — simple chronological audit trail of admin actions. Joined against
    # users for display; LEFT JOIN so a row still shows (with a blank name) if the
    # admin_user_id/target_user_id it points at was ever cleared via ON DELETE SET NULL.
    audit_log = db.fetchall(
        """
        SELECT
            aal.created_at,
            aal.action,
            aal.detail,
            admin_u.email AS admin_email,
            target_u.email AS target_email
        FROM admin_audit_log aal
        LEFT JOIN users admin_u ON admin_u.id = aal.admin_user_id
        LEFT JOIN users target_u ON target_u.id = aal.target_user_id
        ORDER BY aal.created_at DESC
        LIMIT 200
        """
    )

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "instance_health": instance_health,
            "data_quality": data_quality,
            "invites": invites_view,
            "users": users_view,
            "google_status": _provider_status("google"),
            "discord_status": _provider_status("discord"),
            "default_hidden_tags": ", ".join(default_hidden_tags),
            "audit_log": audit_log,
            "schedule": schedule,
            "days_of_week": DAYS_OF_WEEK,
            "day_labels": DAY_LABELS,
            "next_daily_sync": _next_run_time("daily_sync"),
            "next_recommender": _next_run_time("weekly_recommender"),
            "saved": saved,
        },
    )


@app.post("/admin/invites")
def admin_invites_create(request: Request, email: str = Form(...)):
    denied = _require_admin(request)
    if denied:
        return denied

    admin_user = get_current_user(request)
    email = email.strip().lower()
    db.execute(
        "INSERT INTO invites (email, invited_by) VALUES (%s, %s) ON CONFLICT (email) DO NOTHING",
        (email, admin_user["id"]),
    )
    _log_admin_action(admin_user["id"], "invite_created", detail=f"email={email}")
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/invites/{invite_id}/resend")
def admin_invites_resend(request: Request, invite_id: int):
    """Issue #231 — reissue a fresh invite: pushes expires_at another
    INVITE_EXPIRY_DAYS out and clears revoked_at, so this also works to
    un-revoke an invite the admin changes their mind about. Guarded WHERE
    accepted_at IS NULL, same defense-in-depth shape as pat.revoke_token /
    sessions.revoke_session — resending an already-accepted invite doesn't mean
    anything (that email is already a real account)."""
    denied = _require_admin(request)
    if denied:
        return denied

    admin_user = get_current_user(request)
    row = db.execute_returning(
        "UPDATE invites SET expires_at = now() + make_interval(days => %s), revoked_at = NULL "
        "WHERE id = %s AND accepted_at IS NULL RETURNING email",
        (INVITE_EXPIRY_DAYS, invite_id),
    )
    if not row:
        return HTMLResponse(
            "<h1>Invite not found or already accepted</h1><p><a href=\"/admin\">Back to admin</a></p>",
            status_code=404,
        )
    _log_admin_action(admin_user["id"], "invite_resent", detail=f"email={row['email']}")
    return RedirectResponse(url="/admin?saved=invite_resent", status_code=303)


@app.post("/admin/invites/{invite_id}/revoke")
def admin_invites_revoke(request: Request, invite_id: int):
    """Issue #231 — invalidate an outstanding invite before it's used. Guarded
    WHERE accepted_at IS NULL AND revoked_at IS NULL: revoking an accepted invite
    is meaningless (deactivate the account instead, via the Users tab), and
    revoking an already-revoked one is just a no-op we don't need to re-log."""
    denied = _require_admin(request)
    if denied:
        return denied

    admin_user = get_current_user(request)
    row = db.execute_returning(
        "UPDATE invites SET revoked_at = now() "
        "WHERE id = %s AND accepted_at IS NULL AND revoked_at IS NULL RETURNING email",
        (invite_id,),
    )
    if not row:
        return HTMLResponse(
            "<h1>Invite not found, already accepted, or already revoked</h1><p><a href=\"/admin\">Back to admin</a></p>",
            status_code=404,
        )
    _log_admin_action(admin_user["id"], "invite_revoked", detail=f"email={row['email']}")
    return RedirectResponse(url="/admin?saved=invite_revoked", status_code=303)


@app.post("/admin/privacy-defaults")
def admin_privacy_defaults(request: Request, default_hidden_tags: str = Form("")):
    """Instance-wide default hidden-tags list — see app/privacy.py. Unioned with
    each user's own hidden tags, never replaces them."""
    denied = _require_admin(request)
    if denied:
        return denied

    admin_user = get_current_user(request)
    tags = [t.strip() for t in default_hidden_tags.split(",") if t.strip()]
    _instance_config_set("default_hidden_tags", json.dumps(tags))
    _log_admin_action(
        admin_user["id"],
        "privacy_defaults_updated",
        detail=f"default_hidden_tags={', '.join(tags) if tags else '(cleared)'}",
    )
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/oauth-settings")
def admin_oauth_settings(
    request: Request,
    provider: str = Form(...),
    client_id: str = Form(""),
    client_secret: str = Form(""),
):
    denied = _require_admin(request)
    if denied:
        return denied
    if provider not in AUTH_PROVIDERS:
        return JSONResponse({"error": "unknown provider"}, status_code=404)

    changed = []
    if client_id.strip():
        _instance_config_set(f"{provider}_client_id", client_id.strip())
        changed.append("client_id")
    if client_secret.strip():
        _instance_config_set(f"{provider}_client_secret", client_secret.strip())
        changed.append("client_secret")

    admin_user = get_current_user(request)
    # Never log the actual secret/id values — just which fields were touched.
    _log_admin_action(
        admin_user["id"],
        "oauth_settings_updated",
        detail=f"provider={provider}; updated={', '.join(changed) if changed else '(nothing submitted)'}",
    )
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/users/{user_id}/reset-password")
def admin_reset_password(request: Request, user_id: int):
    denied = _require_admin(request)
    if denied:
        return denied

    target = db.fetchone("SELECT * FROM users WHERE id = %s", (user_id,))
    if not target:
        return HTMLResponse("<h1>User not found</h1>", status_code=404)

    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO password_resets (token, user_id, expires_at) VALUES (%s, %s, now() + interval '1 hour')",
        (token, user_id),
    )
    admin_user = get_current_user(request)
    _log_admin_action(
        admin_user["id"],
        "password_reset",
        target_user_id=user_id,
        detail=f"target_email={target['email']}",
    )
    reset_url = request.url_for("auth_reset_password_page", token=token)
    return HTMLResponse(f"""
        <h1>Password reset link for {html.escape(target['email'])}</h1>
        <p>Valid for 1 hour, single use. Copy this link now and send it to them —
        it won't be shown again:</p>
        <p><code>{reset_url}</code></p>
        <p><a href="/admin">Back to admin</a></p>
    """)


@app.post("/admin/users/{user_id}/deactivate")
def admin_deactivate_user(request: Request, user_id: int):
    denied = _require_admin(request)
    if denied:
        return denied

    admin_user = get_current_user(request)
    if admin_user["id"] == user_id:
        return HTMLResponse("<h1>Can't deactivate your own account</h1><p><a href=\"/admin\">Back to admin</a></p>", status_code=400)

    target = db.fetchone("SELECT * FROM users WHERE id = %s", (user_id,))
    if not target:
        return HTMLResponse("<h1>User not found</h1>", status_code=404)

    db.execute("UPDATE users SET is_active = false WHERE id = %s", (user_id,))
    # Issue #82 — now that sessions are server-side, cut off any already-open tab
    # immediately instead of only rejecting it lazily on its next request.
    sessions.revoke_all_sessions(user_id)
    _log_admin_action(
        admin_user["id"],
        "user_deactivated",
        target_user_id=user_id,
        detail=f"target_email={target['email']}",
    )
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/users/{user_id}/reactivate")
def admin_reactivate_user(request: Request, user_id: int):
    denied = _require_admin(request)
    if denied:
        return denied

    target = db.fetchone("SELECT * FROM users WHERE id = %s", (user_id,))
    if not target:
        return HTMLResponse("<h1>User not found</h1>", status_code=404)

    db.execute("UPDATE users SET is_active = true WHERE id = %s", (user_id,))
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/users/{user_id}/disable-2fa")
def admin_disable_2fa(request: Request, user_id: int):
    """Security-review finding (post-#83): there was no path — not even for an
    admin — to disable 2FA for a user who lost both their authenticator AND all
    recovery codes (the admin-mediated password reset above only ever touches
    password_hash/failed_login_attempts/locked_until, never the totp_* columns).
    That was a permanent-lockout trap. Gated the same way every other admin
    user-management action in this app already is (_require_admin + admin_audit_log),
    not a new pattern."""
    denied = _require_admin(request)
    if denied:
        return denied

    target = db.fetchone("SELECT * FROM users WHERE id = %s", (user_id,))
    if not target:
        return HTMLResponse("<h1>User not found</h1>", status_code=404)
    if not target["totp_enabled"]:
        return RedirectResponse(url="/admin", status_code=303)

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM totp_recovery_codes WHERE user_id = %s", (user_id,))
            cur.execute(
                "UPDATE users SET totp_secret = NULL, totp_enabled = false, totp_enabled_at = NULL, "
                "totp_failed_attempts = 0, totp_locked_until = NULL WHERE id = %s",
                (user_id,),
            )
        conn.commit()

    admin_user = get_current_user(request)
    _log_admin_action(
        admin_user["id"],
        "totp_disabled_by_admin",
        target_user_id=user_id,
        detail=f"target_email={target['email']}",
    )
    return RedirectResponse(url="/admin", status_code=303)


def _require_user(request: Request):
    """Returns (user, None) if authenticated, or (None, redirect) to send back if not.
    For page routes — browser-friendly redirect to the login page, or to registration
    on a fresh install where there's no one to log in as yet."""
    user = get_current_user(request)
    if not user:
        if _no_users_exist():
            return None, RedirectResponse(url="/auth/register", status_code=303)
        return None, RedirectResponse(url="/auth/login", status_code=303)
    return user, None


def _require_user_api(request: Request):
    """Same as _require_user, but 401 JSON instead of a redirect — for /api/* routes,
    where a browser-followed redirect on a fetch() call would return HTML where the
    caller expects JSON."""
    user = get_current_user(request)
    if not user:
        return None, JSONResponse({"error": "not authenticated"}, status_code=401)
    return user, None


SNOOZE_DAYS = 30  # v1 fixed duration for "not now" (issue #75) — no picker yet;
                  # revisit only if a fixed 30 days turns out to not be enough.


def _is_uninformative_reason(reason: dict | None) -> bool:
    """Issue #178: a cold-start user (empty taste profile) gets every seasonal-digest
    candidate scored at exactly 0 with a fully-null `reason` — real, not broken, but
    rendering that as a literal "0%" badge with match reasoning reads as a bug. This
    flags rows where `reason` carries no actual signal (no genre/tag/studio match, no
    cross-user corroboration) so recommendations() can swap the numeric score badge for
    honest framing instead. Read/render-time only — never touches score_and_store()'s
    scoring math or what gets written to recommendation_scores."""
    reason = reason or {}
    return (
        not reason.get("matched_genres")
        and not reason.get("matched_tags")
        and not reason.get("matched_studio")
        and not reason.get("cross_user_count")
    )


def _compute_match_percentages(entries: list[dict]) -> None:
    """Issue #227 — Netflix-style "% match" display: a read-time min-max
    normalization of `rec_score` across exactly the recommendations visible on
    this page load. Mutates each entry in place, setting `match_pct`.

    Deliberately a *second*, independent normalization from the one
    score_and_store() already does at write time (scripts/run_recommender.py):
    that one divides every scored candidate by the best *scored* candidate
    (max-only, no floor) before per-source LIMIT 100 and before dismiss/snooze
    filtering ever happen — so its denominator can include candidates the
    user never actually sees on this page. This one is min-max over exactly
    what's on screen right now, so the weakest visible pick reads as a real
    0% instead of being flattered by a ceiling set by a candidate that got
    filtered out. The two percentages are allowed to differ; they're
    intentionally not the same number shown twice — see issue #227's "next to
    (not replacing) the existing internal score" framing. Purely a display
    transform: never writes to recommendation_scores, never touches ranking.

    Uninformative (cold-start, issue #178) entries are excluded from the
    min/max range and get `match_pct = None` — same "no real signal" treatment
    the existing score badge already gives them via `uninformative`."""
    informative_scores = [e["rec_score"] for e in entries if not e["uninformative"]]
    if not informative_scores:
        for e in entries:
            e["match_pct"] = None
        return
    lo, hi = min(informative_scores), max(informative_scores)
    for e in entries:
        if e["uninformative"]:
            e["match_pct"] = None
        elif hi == lo:
            # Every visible informative pick is tied — each is the best you've got.
            e["match_pct"] = 100
        else:
            e["match_pct"] = round((e["rec_score"] - lo) / (hi - lo) * 100)


def _fetch_visible_recommendations(user_id: int) -> list[dict]:
    """Recommendation rows visible to `user_id`: not permanently dismissed, and not
    currently snoozed (issue #75). Broken out of recommendations() so the exclusion
    logic is directly testable against a real Postgres without needing to fake a
    full Request/session — see tests/test_recommendation_snooze.py.

    Issue #13: `rs.source` distinguishes the original similarity-based path from
    the new seasonal discovery digest. The LIMIT is applied per-source (via
    ROW_NUMBER) rather than globally, so a season with a lot of new releases can't
    starve out the similarity picks (or vice versa) — both get their own top-100
    budget, sorted by score within each.

    Issue #254: `from_planning` is a separate, live signal from `source` — it's not
    stored on `recommendation_scores` at all, since run_recommender.py's candidate
    fetch/scoring logic deliberately isn't touched by this (presentation-only fix).
    A candidate discovered via the user's own PLANNING list gets written with
    source='similarity', identically to a genuine AniList-recommendations pick —
    that's still correct for scoring purposes, but reads on the page as "the app
    doesn't know I already have this". The LEFT JOIN below checks the *current*
    library_entries status instead of freezing a label at the last recommender run,
    so it stays accurate even if the user later removes the item from Planning."""
    return db.fetchall(
        """
        SELECT id, title_english, title_romaji, cover_image_url, format, episodes,
               average_score, genres, season, season_year, rec_score, reason, source,
               from_planning
        FROM (
            SELECT
                a.id,
                a.title_english,
                a.title_romaji,
                a.cover_image_url,
                a.format,
                a.episodes,
                a.average_score,
                a.genres,
                a.season,
                a.season_year,
                rs.score  AS rec_score,
                rs.reason,
                rs.source,
                (le.id IS NOT NULL) AS from_planning,
                ROW_NUMBER() OVER (PARTITION BY rs.source ORDER BY rs.score DESC) AS rn
            FROM recommendation_scores rs
            JOIN anime a ON a.id = rs.anime_id
            LEFT JOIN library_entries le
                ON le.anime_id = a.id AND le.user_id = rs.user_id AND le.status = 'PLANNING'
            WHERE rs.dismissed = false
              AND (rs.snoozed_until IS NULL OR rs.snoozed_until <= now())
              AND rs.user_id = %s
        ) ranked
        WHERE rn <= 100
        ORDER BY source, rec_score DESC
        """,
        (user_id,),
    )


@app.get("/recommendations", response_class=HTMLResponse)
def recommendations(request: Request):
    user, denied = _require_user(request)
    if denied:
        return denied

    rows = _fetch_visible_recommendations(user["id"])
    # Build genre → completed_anime index for "similar to" lookup
    completed = db.fetchall(
        """
        SELECT a.id, a.title_english, a.title_romaji, a.genres
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        WHERE le.status = 'COMPLETED' AND a.genres IS NOT NULL AND le.user_id = %s
        """,
        (user["id"],),
    )
    comp_genres: list[tuple[int, str, list]] = [
        (r["id"], r["title_english"] or r["title_romaji"], r["genres"] or [])
        for r in completed
    ]

    entries = []
    seasonal_count = 0
    for row in rows:
        rec_genres = set(row["genres"] or [])
        best_title, best_overlap = None, 0
        for _, title, cg in comp_genres:
            overlap = len(rec_genres & set(cg))
            if overlap > best_overlap:
                best_overlap, best_title = overlap, title
        entry = dict(row)
        entry["similar_to"] = best_title if best_overlap >= 2 else None
        # Issue #13 — "Fall 2026"-style badge for seasonal-digest cards.
        if entry.get("source") == "seasonal" and entry.get("season"):
            entry["season_label"] = f"{entry['season'].title()} {entry['season_year']}"
            seasonal_count += 1
        else:
            entry["season_label"] = None
        # Issue #178 — cold-start rows (empty taste profile, no library history)
        # carry a real but uninformative score=0/reason=null pair. Flag it here so
        # the template can swap the "0%" score badge for honest framing instead.
        entry["uninformative"] = _is_uninformative_reason(entry.get("reason"))
        entries.append(entry)

    # Issue #227 — normalized 0-100 "% match" display, alongside (not replacing)
    # the rec_score badge above. See _compute_match_percentages()'s docstring for
    # why this is a second, independent normalization rather than a re-render of
    # the same number.
    _compute_match_percentages(entries)

    return templates.TemplateResponse(
        request,
        "recommendations.html",
        {"entries": entries, "seasonal_count": seasonal_count},
    )


@app.post("/recommendations/{anime_id}/dismiss")
async def dismiss(anime_id: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    reason = None
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        reason = body.get("reason") or None
        db.execute(
            "UPDATE recommendation_scores SET dismissed = true, dismiss_reason = %s "
            "WHERE anime_id = %s AND user_id = %s",
            (reason, anime_id, user["id"]),
        )
        return JSONResponse({"ok": True})
    db.execute(
        "UPDATE recommendation_scores SET dismissed = true WHERE anime_id = %s AND user_id = %s",
        (anime_id, user["id"]),
    )
    return RedirectResponse(url="/recommendations", status_code=303)


@app.post("/recommendations/{anime_id}/snooze")
async def snooze(anime_id: int, request: Request):
    """Time-boxed "not now" (issue #75) — distinct from the permanent dismiss above.
    Hides the entry from the recommendations view for SNOOZE_DAYS; it resurfaces
    automatically once snoozed_until passes, and a recommender rebuild in the
    meantime doesn't reset or shorten it (see run_recommender.py's score_and_store,
    which never touches snoozed_until, same as it never touches dismissed)."""
    user, denied = _require_user_api(request)
    if denied:
        return denied

    db.execute(
        "UPDATE recommendation_scores "
        "SET snoozed_until = now() + make_interval(days => %s) "
        "WHERE anime_id = %s AND user_id = %s",
        (SNOOZE_DAYS, anime_id, user["id"]),
    )
    return JSONResponse({"ok": True})


def _group_consecutive_episodes(episodes: list[dict]) -> list[dict]:
    """Collapse a status-homogeneous, episode_number-ascending list of
    filler_episode_cache rows into consecutive runs (e.g. episodes 12,13,14,15
    -> one range "12-15"), per issue #300's acceptance criteria ("expandable
    filler/mixed episode-number lists, ranges where consecutive"). Each
    returned group carries a `label` for display plus one representative
    episode's citation_url/citation_description/status_note (the first
    episode in the run that actually has one, falling back to the run's first
    episode) for the tooltip-on-expand — different episodes in the same run
    can in principle carry different citations, but showing one representative
    source per collapsed range is the reasonable trade-off for a compact
    display; the full episode list is still on the group if a caller ever
    needs it."""
    groups: list[dict] = []
    for ep in episodes:
        if groups and ep["episode_number"] == groups[-1]["end"] + 1:
            groups[-1]["end"] = ep["episode_number"]
            groups[-1]["episodes"].append(ep)
        else:
            groups.append({"start": ep["episode_number"], "end": ep["episode_number"], "episodes": [ep]})

    for g in groups:
        g["label"] = str(g["start"]) if g["start"] == g["end"] else f"{g['start']}-{g['end']}"
        rep = next(
            (e for e in g["episodes"] if e.get("citation_url") or e.get("citation_description")),
            g["episodes"][0],
        )
        g["citation_url"] = rep.get("citation_url")
        g["citation_description"] = rep.get("citation_description")
        g["status_note"] = rep.get("status_note")
    return groups


def _get_filler_data(anime_id: int) -> dict | None:
    """Filler/canon episode breakdown for the notes page (issue #300), sourced
    from #299's three-table cache (filler_episode_cache / filler_sync_state /
    filler_data_license — see schema.sql for the full design rationale).

    Returns None when filler_sync_state has no row at all for this anime —
    the sync job has never even attempted this title, which the caller renders
    as no section at all (not even an empty state), per #300's scope. This is
    distinct from every other outcome below, which the sync job HAS attempted
    and so are all worth surfacing:
      - "no_match": a row exists but afp_series_id IS NULL — AniFillerPedia has
        no series matching this title (yet). Calm "not researched" state.
      - "matched_empty": afp_series_id is set (a real AniFillerPedia series
        match) but filler_episode_cache has zero rows for it — matched, but
        nobody's researched any of its episodes on AniFillerPedia's side yet.
        Also a calm "not researched" state, worded slightly differently since
        we do at least know which AniFillerPedia series this is.
      - "matched": real episode data exists. Counts + consecutive-range
        groups per status, ready for the template's disclosure widgets."""
    sync_state = db.fetchone(
        "SELECT afp_series_id, last_checked_at FROM filler_sync_state WHERE anime_id = %s",
        (anime_id,),
    )
    if not sync_state:
        return None

    if sync_state["afp_series_id"] is None:
        return {"state": "no_match", "last_checked_at": sync_state["last_checked_at"]}

    rows = db.fetchall(
        """
        SELECT episode_number, status, status_note, citation_url, citation_description
        FROM filler_episode_cache
        WHERE anime_id = %s
        ORDER BY episode_number
        """,
        (anime_id,),
    )
    if not rows:
        return {"state": "matched_empty", "last_checked_at": sync_state["last_checked_at"]}

    by_status: dict[str, list[dict]] = {"canon": [], "filler": [], "mixed": []}
    for r in rows:
        by_status[r["status"]].append(dict(r))

    return {
        "state": "matched",
        "last_checked_at": sync_state["last_checked_at"],
        "counts": {k: len(v) for k, v in by_status.items()},
        "total_researched": len(rows),
        "groups": {k: _group_consecutive_episodes(v) for k, v in by_status.items()},
    }


def _get_filler_license() -> dict | None:
    """Cached CC BY-NC-SA attribution text for AniFillerPedia (#299's GET
    /license snapshot) — no live call needed per page render. None only if
    #299's sync has literally never run once against the singleton row."""
    row = db.fetchone(
        "SELECT license_name, attribution_notice FROM filler_data_license WHERE id = 1"
    )
    return dict(row) if row else None


def _also_watching(anime_id: int, viewer_user_id: int, genres: list[str] | None) -> list[dict]:
    """Other same-instance users who have this anime in their library, respecting
    #26's privacy controls — see app/privacy.py. Static/on-demand only, computed
    when this page is viewed; never pushed, polled, or notified (#22's scope)."""
    rows = db.fetchall(
        """
        SELECT u.id, u.email, u.display_name, le.status, pn.personal_tags
        FROM library_entries le
        JOIN users u ON u.id = le.user_id
        LEFT JOIN personal_notes pn ON pn.anime_id = le.anime_id AND pn.user_id = le.user_id
        WHERE le.anime_id = %s AND le.user_id != %s
        ORDER BY u.email
        """,
        (anime_id, viewer_user_id),
    )
    result = []
    for r in rows:
        hidden_tags = privacy.get_hidden_tags(r["id"])
        if privacy.entry_hidden(genres, r["personal_tags"], hidden_tags):
            continue
        result.append({"name": privacy.display_name(r), "status": r["status"]})
    return result


@app.get("/anime/{anime_id}/notes", response_class=HTMLResponse)
def notes_form(request: Request, anime_id: int, back: str = "WATCHING"):
    user, denied = _require_user(request)
    if denied:
        return denied

    anime = db.fetchone(
        """
        SELECT a.id, a.title_english, a.title_romaji, a.cover_image_url, le.status,
               a.trailer_yt_id, a.relations, a.genres, a.average_score, le.score,
               le.repeat_count
        FROM anime a
        LEFT JOIN library_entries le ON le.anime_id = a.id AND le.user_id = %s
        WHERE a.id = %s
        """,
        (user["id"], anime_id),
    )
    notes = db.fetchone(
        "SELECT * FROM personal_notes WHERE anime_id = %s AND user_id = %s",
        (anime_id, user["id"]),
    )

    current_repeat_count = anime["repeat_count"] if anime and anime["repeat_count"] else 0
    rewatch_notes = _get_rewatch_notes(user["id"], anime_id, current_repeat_count)

    also_watching = _also_watching(anime_id, user["id"], anime["genres"] if anime else [])

    filler = _get_filler_data(anime_id) if anime else None
    filler_license = _get_filler_license() if filler else None

    trailer = None
    if anime and anime["trailer_yt_id"]:
        vid = anime["trailer_yt_id"]
        trailer = {
            "url": f"https://www.youtube.com/watch?v={vid}",
            "thumbnail": f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
        }

    related = list(anime["relations"] if anime and anime["relations"] else [])
    related.sort(key=lambda r: _RELATION_ORDER.index(r["relation_type"])
                 if r["relation_type"] in _RELATION_ORDER else 99)

    if related:
        in_library = {
            row["anime_id"]
            for row in db.fetchall(
                "SELECT anime_id FROM library_entries WHERE anime_id = ANY(%s) AND user_id = %s",
                ([r["id"] for r in related], user["id"]),
            )
        }
        for r in related:
            r["in_library"] = r["id"] in in_library

    return templates.TemplateResponse(
        request,
        "notes.html",
        {"anime": anime, "notes": notes, "back": back,
         "trailer": trailer, "related": related, "also_watching": also_watching,
         "rewatch_notes": rewatch_notes, "mood_tags": MOOD_TAGS,
         "filler": filler, "filler_license": filler_license},
    )


@app.post("/anime/{anime_id}/notes")
def save_notes(
    request: Request,
    anime_id: int,
    drop_reason: str = Form(""),
    notes: str = Form(""),
    personal_tags: str = Form(""),
    mood: list[str] = Form([]),
    watch_next_priority: str = Form(""),
    anilist_id_override: str = Form(""),
    back: str = Form("WATCHING"),
):
    user, denied = _require_user(request)
    if denied:
        return denied

    drop_reason_val = drop_reason.strip() or None
    notes_val = notes.strip() or None
    tags = [t.strip() for t in personal_tags.split(",") if t.strip()]
    mood_val = _filter_mood_tags(mood)
    try:
        priority = int(watch_next_priority.strip()) if watch_next_priority.strip() else None
    except ValueError:
        priority = None
    try:
        al_override = int(anilist_id_override.strip()) if anilist_id_override.strip() else None
    except ValueError:
        al_override = None

    _upsert_personal_notes(user["id"], anime_id, drop_reason_val, notes_val, tags, priority, al_override, mood_val)

    if drop_reason_val:
        error = _apply_status_change(user, anime_id, "DROPPED")
        if error:
            log.error("Drop-via-notes-page status update failed for anime %s: %s", anime_id, error)

    return RedirectResponse(url=f"/?status={back}", status_code=303)


@app.post("/api/anime/{anime_id}/notes")
async def save_notes_api(anime_id: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    drop_reason_val = (body.get("drop_reason") or "").strip() or None
    notes_val = (body.get("notes") or "").strip() or None
    tags_raw = body.get("personal_tags") or ""
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    mood_val = _filter_mood_tags(body.get("mood") or [])
    priority_raw = body.get("watch_next_priority")
    try:
        priority = int(priority_raw) if priority_raw not in (None, "") else None
    except (ValueError, TypeError):
        priority = None

    al_override_raw = body.get("anilist_id_override")
    try:
        al_override = int(al_override_raw) if al_override_raw not in (None, "") else None
    except (ValueError, TypeError):
        al_override = None

    _upsert_personal_notes(user["id"], anime_id, drop_reason_val, notes_val, tags, priority, al_override, mood_val)
    return JSONResponse({"ok": True})


def _upsert_personal_notes(
    user_id: int,
    anime_id: int,
    drop_reason_val: str | None,
    notes_val: str | None,
    tags: list,
    priority,
    al_override,
    mood: list | None = None,
) -> None:
    """Full-replace upsert into personal_notes — the actual write logic behind both
    the notes form route (save_notes) and the JSON API route (save_notes_api)
    above, and reused as-is by the MCP update_personal_notes write tool (issue
    #208) rather than being reimplemented there. Not a partial patch: every column
    listed is overwritten with exactly what's passed, mirroring the existing form/
    API semantics where the caller always submits the complete set of fields —
    a field left unset by the caller clears that column, it doesn't leave it
    alone. `mood` (issue #218) follows the same full-replace rule as `tags` —
    callers should already have run it through _filter_mood_tags, this doesn't
    re-validate."""
    db.execute(
        """
        INSERT INTO personal_notes (user_id, anime_id, drop_reason, personal_tags, mood_tags, notes, watch_next_priority, anilist_id_override)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, anime_id) DO UPDATE SET
            drop_reason = EXCLUDED.drop_reason,
            personal_tags = EXCLUDED.personal_tags,
            mood_tags = EXCLUDED.mood_tags,
            notes = EXCLUDED.notes,
            watch_next_priority = EXCLUDED.watch_next_priority,
            anilist_id_override = EXCLUDED.anilist_id_override,
            updated_at = now()
        """,
        (user_id, anime_id, drop_reason_val, json.dumps(tags), json.dumps(mood or []), notes_val, priority, al_override),
    )


def _get_rewatch_notes(user_id: int, anime_id: int, current_repeat_count: int) -> list:
    """One entry per rewatch that's happened so far (repeat_count 1..N), pre-filled
    with any existing note for that rewatch (blank if none yet). The original watch
    (repeat_count 0) keeps using personal_notes.notes — not included here. Returns
    [] for an anime with no rewatches, so existing single-note behavior is unaffected."""
    rows = db.fetchall(
        "SELECT repeat_count, note FROM rewatch_notes WHERE anime_id = %s AND user_id = %s ORDER BY repeat_count",
        (anime_id, user_id),
    )
    by_count = {r["repeat_count"]: r["note"] for r in rows}
    return [
        {"repeat_count": n, "note": by_count.get(n, "")}
        for n in range(1, current_repeat_count + 1)
    ]


def _save_rewatch_note(user_id: int, anime_id: int, repeat_count: int, note: str) -> bool:
    """Attach a note to one specific rewatch (issue #14), separate from the
    general/original note on personal_notes.notes. Only valid for a rewatch
    that's actually happened — repeat_count must be between 1 and the user's
    current library_entries.repeat_count for this anime, inclusive. Blank note
    deletes any existing row for that rewatch (lets a note be cleared).
    Returns True if the write was applied, False if repeat_count was out of range
    (no library entry, or asking for a rewatch that hasn't happened yet)."""
    if repeat_count < 1:
        return False

    entry = db.fetchone(
        "SELECT repeat_count FROM library_entries WHERE anime_id = %s AND user_id = %s",
        (anime_id, user_id),
    )
    if not entry or not entry["repeat_count"] or repeat_count > entry["repeat_count"]:
        return False

    note_val = note.strip()
    if note_val:
        db.execute(
            """
            INSERT INTO rewatch_notes (user_id, anime_id, repeat_count, note)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, anime_id, repeat_count) DO UPDATE SET
                note = EXCLUDED.note,
                updated_at = now()
            """,
            (user_id, anime_id, repeat_count, note_val),
        )
    else:
        db.execute(
            "DELETE FROM rewatch_notes WHERE user_id = %s AND anime_id = %s AND repeat_count = %s",
            (user_id, anime_id, repeat_count),
        )
    return True


@app.post("/anime/{anime_id}/rewatch-notes/{repeat_count}")
def save_rewatch_note(
    request: Request,
    anime_id: int,
    repeat_count: int,
    note: str = Form(""),
    back: str = Form("WATCHING"),
):
    user, denied = _require_user(request)
    if denied:
        return denied

    _save_rewatch_note(user["id"], anime_id, repeat_count, note)

    return RedirectResponse(url=f"/anime/{anime_id}/notes?back={back}", status_code=303)


def _get_episode_note_and_quote(user_id: int, anime_id: int, episode_number: int) -> dict:
    """Fetch both the freeform note and the favorite-quote / memorable-scene text
    (issue #220) for one episode in a single lookup. Deliberately a single lookup
    rather than _get_rewatch_notes' whole-range list — an anime can have hundreds
    of episodes, so the card only ever asks for the one episode it currently needs
    (the progress-stepper's current value) instead of pre-fetching a blank-filled
    row per episode."""
    row = db.fetchone(
        "SELECT note, memorable_quote FROM episode_notes WHERE user_id = %s AND anime_id = %s AND episode_number = %s",
        (user_id, anime_id, episode_number),
    )
    if not row:
        return {"note": "", "quote": ""}
    return {"note": row["note"] or "", "quote": row["memorable_quote"] or ""}


def _get_episode_note(user_id: int, anime_id: int, episode_number: int) -> str:
    """Fetch the note text for one episode (issue #210), or '' if none exists yet."""
    return _get_episode_note_and_quote(user_id, anime_id, episode_number)["note"]


def _save_episode_note(user_id: int, anime_id: int, episode_number: int, note: str, quote: str = "") -> bool:
    """Attach a note and/or a favorite-quote / memorable-scene text (issue #220) to
    one specific episode — same shape/rules as _save_rewatch_note. Only valid for
    an episode that's actually been watched — episode_number must be between 1 and
    the user's current library_entries.progress for this anime, inclusive. `note`
    and `quote` are independent fields on the same row; the row is deleted only
    when *both* are blank, so a quote-only entry (or a note-only entry) survives
    on its own. Returns True if the write was applied, False if episode_number was
    out of range (no library entry, or noting an episode not yet reached)."""
    if episode_number < 1:
        return False

    entry = db.fetchone(
        "SELECT progress FROM library_entries WHERE anime_id = %s AND user_id = %s",
        (anime_id, user_id),
    )
    if not entry or not entry["progress"] or episode_number > entry["progress"]:
        return False

    note_val = note.strip()
    quote_val = quote.strip()
    if note_val or quote_val:
        db.execute(
            """
            INSERT INTO episode_notes (user_id, anime_id, episode_number, note, memorable_quote)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id, anime_id, episode_number) DO UPDATE SET
                note = EXCLUDED.note,
                memorable_quote = EXCLUDED.memorable_quote,
                updated_at = now()
            """,
            (user_id, anime_id, episode_number, note_val, quote_val or None),
        )
    else:
        db.execute(
            "DELETE FROM episode_notes WHERE user_id = %s AND anime_id = %s AND episode_number = %s",
            (user_id, anime_id, episode_number),
        )
    return True


@app.get("/api/anime/{anime_id}/episode-notes/{episode_number}")
def get_episode_note_api(anime_id: int, episode_number: int, request: Request):
    """Read-only lookup backing the note popover's prefill (note + favorite-quote/
    memorable-scene text, issue #220) and the auto-suggest prompt's "does this
    episode already have a note?" check (see script.js) — inline, JSON, no page
    navigation, unlike rewatch notes' whole-page form."""
    user, denied = _require_user_api(request)
    if denied:
        return denied
    return JSONResponse(_get_episode_note_and_quote(user["id"], anime_id, episode_number))


@app.post("/api/anime/{anime_id}/episode-notes/{episode_number}")
async def save_episode_note_api(anime_id: int, episode_number: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    note = body.get("note")
    if note is None:
        note = ""
    if not isinstance(note, str):
        return JSONResponse({"error": "note must be a string"}, status_code=400)

    quote = body.get("quote")
    if quote is None:
        quote = ""
    if not isinstance(quote, str):
        return JSONResponse({"error": "quote must be a string"}, status_code=400)

    applied = _save_episode_note(user["id"], anime_id, episode_number, note, quote)
    if not applied:
        return JSONResponse({"error": "episode not yet watched"}, status_code=400)
    return JSONResponse({"ok": True, "note": note.strip(), "quote": quote.strip()})


_RELATION_ORDER = ["PREQUEL", "SEQUEL", "PARENT", "SIDE_STORY", "SPIN_OFF",
                   "ALTERNATIVE", "COMPILATION", "CONTAINS", "SUMMARY", "OTHER"]


@app.get("/upcoming", response_class=HTMLResponse)
def upcoming(
    request: Request,
    week_offset: int = 0,
    month_offset: int = 0,
    # Named date_str (not `date`) to avoid shadowing the `date` class imported from
    # datetime at module level, which _add_months and the month-grid code below both
    # call directly as `date(...)` — a same-named parameter shadows that for the
    # entire function body, silently breaking every date(...) call in this route
    # (caught by the existing month/week-grid test suite, not by anything date-filter
    # specific). alias="date" keeps the public query string as ?date=YYYY-MM-DD.
    date_str: str = Query(default=None, alias="date"),
):
    user, denied = _require_user(request)
    if denied:
        return denied

    # airing_schedule_cache only ever holds not-yet-aired rows (rows are deleted the
    # moment an episode airs — see stats.html's comment on the same table), so a week
    # before the current one can never have anything to show. Clamp rather than let
    # Prev walk into a guaranteed-empty grid. Same reasoning applies to month_offset
    # below (a month prior to the current one is guaranteed empty since every day in
    # it is strictly in the past).
    week_offset = max(0, week_offset)
    month_offset = max(0, month_offset)

    tz_name = config.get(user["id"], "timezone")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = timezone.utc

    rows = db.fetchall(
        """
        SELECT
            a.id,
            a.title_english,
            a.title_romaji,
            a.cover_image_url,
            a.episodes,
            le.progress,
            asc2.episode,
            asc2.airing_at
        FROM airing_schedule_cache asc2
        JOIN anime a ON a.id = asc2.anime_id
        JOIN library_entries le ON le.anime_id = a.id
        WHERE asc2.airing_at > now() AND le.user_id = %s
        ORDER BY asc2.airing_at
        """,
        (user["id"],),
    )

    now = datetime.now(tz=timezone.utc)
    entries = []
    for row in rows:
        entry = dict(row)
        airing_local = row["airing_at"].astimezone(tz)
        entry["airing_local"] = airing_local
        entry["tz_abbr"] = airing_local.strftime("%Z")
        delta = row["airing_at"] - now
        days = delta.days
        hours = delta.seconds // 3600
        if days == 0 and delta.total_seconds() < 86400:
            if hours == 0:
                entry["relative"] = "in less than an hour"
            elif hours == 1:
                entry["relative"] = "in 1 hour"
            else:
                entry["relative"] = f"in {hours} hours"
            entry["group"] = "Today"
        elif days == 1:
            entry["relative"] = "tomorrow"
            entry["group"] = "Tomorrow"
        elif days <= 7:
            entry["relative"] = f"in {days} days"
            entry["group"] = "This week"
        else:
            weeks = days // 7
            entry["relative"] = f"in {days} days"
            entry["group"] = f"In {weeks} week{'s' if weeks > 1 else ''}"
        entries.append(entry)

    # Filler-episode tag (issue #302, reading #299's filler_episode_cache). Per
    # #302's acceptance criteria, only a known 'filler' status gets a visible
    # treatment — 'canon', 'mixed', and "no cache row at all" (unknown) all render
    # identically to today, so this only needs a lookup set of the (anime_id,
    # episode) pairs that are actually 'filler', not the full status per entry.
    # A single ANY(%s)-scoped query (matching this file's existing ANY(%s)
    # pattern, e.g. the airing_schedule_cache widen query below) avoids one query
    # per entry.
    filler_anime_ids = list({e["id"] for e in entries})
    filler_pairs = set()
    if filler_anime_ids:
        filler_rows = db.fetchall(
            """
            SELECT anime_id, episode_number
            FROM filler_episode_cache
            WHERE anime_id = ANY(%s) AND status = 'filler'
            """,
            (filler_anime_ids,),
        )
        filler_pairs = {(r["anime_id"], r["episode_number"]) for r in filler_rows}
    for entry in entries:
        entry["is_filler"] = (entry["id"], entry["episode"]) in filler_pairs

    # Day-filtered list view (issue #277) — clicking a day cell or its "+N more"
    # overflow text in the month grid links here via `?date=YYYY-MM-DD`, reusing
    # the same `entries` list (and the same entry-card markup) rather than a
    # separate query or a new modal/popover. Invalid/missing date falls back to
    # None, which the template treats identically to no filter at all.
    date_filter = None
    if date_str:
        try:
            date_filter = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            date_filter = None

    # Named date_filter_entries, not day_entries — the month-grid loop below
    # declares its own local `day_entries` per cell, which would otherwise
    # silently shadow/overwrite this one before it reaches the template context.
    date_filter_entries = (
        [e for e in entries if e["airing_local"].date() == date_filter]
        if date_filter is not None
        else None
    )

    # Weekly Mon-Sun broadcast-calendar grid — bounded to exactly one real calendar
    # week (issue #256), reusing the same `entries` already built above. No change to
    # how airing_schedule_cache itself is synced/cached, and no change to `entries` /
    # the List view that consumes it.
    today_local = now.astimezone(tz).date()
    monday_this_week = today_local - timedelta(days=today_local.weekday())
    week_start = monday_this_week + timedelta(weeks=week_offset)
    week_end = week_start + timedelta(days=7)  # exclusive — next Monday

    weekday_names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    week_grid = [
        {
            "name": name,
            "date": week_start + timedelta(days=idx),
            "entries": [],
            "is_today": (week_start + timedelta(days=idx)) == today_local,
        }
        for idx, name in enumerate(weekday_names)
    ]
    for entry in entries:
        entry_date = entry["airing_local"].date()
        if week_start <= entry_date < week_end:
            week_grid[entry_date.weekday()]["entries"].append(entry)

    # Month calendar grid (issue #257) — third toggle alongside List and Weekly
    # grid, reusing the exact same `entries` list built above rather than a
    # separate query. No widening of the SQL query above turned out to be needed:
    # it already has no upper bound on airing_at, so every future entry regardless
    # of how far out is already present in `entries`; and airing_schedule_cache
    # never holds already-aired rows (see the week_offset clamp comment above), so
    # days earlier in the displayed month than today are correctly guaranteed
    # empty rather than needing a past-data fetch — resolving the open question
    # in #257 in favor of "render empty" by construction, not by a UI choice.
    def _add_months(d: date, months: int) -> date:
        total = d.month - 1 + months
        year = d.year + total // 12
        month = total % 12 + 1
        return date(year, month, 1)

    first_of_this_month = today_local.replace(day=1)
    month_start = _add_months(first_of_this_month, month_offset)
    next_month_start = _add_months(month_start, 1)

    # Leading/trailing blank cells for days outside the displayed month, matching
    # typical calendar UI conventions (#257) — rendered as genuinely blank (no date
    # number, no chips), matching the linked mockup's Option B rather than showing
    # muted adjacent-month content.
    leading_blanks = month_start.weekday()  # Monday=0 .. Sunday=6
    days_in_month = (next_month_start - month_start).days
    trailing_blanks = (7 - (leading_blanks + days_in_month) % 7) % 7
    total_cells = leading_blanks + days_in_month + trailing_blanks
    grid_start = month_start - timedelta(days=leading_blanks)

    entries_by_date = {}
    for entry in entries:
        entries_by_date.setdefault(entry["airing_local"].date(), []).append(entry)

    # Cap on chips rendered per day cell before an overflow indicator takes over.
    # 3 was picked to comfortably fit the month cell's compact height (~5.5rem,
    # see .upcoming-month-cell in style.css) alongside the date number without the
    # cell growing tall enough to break the 7-column grid's row alignment — a
    # busy day (a simulcast night with several shows airing) realistically has at
    # most a handful of entries, so "+N more" stays rare rather than the common case.
    MONTH_CHIP_CAP = 3

    month_grid = []
    cursor = grid_start
    for _ in range(total_cells // 7):
        week_cells = []
        for _ in range(7):
            in_month = month_start <= cursor < next_month_start
            day_entries = entries_by_date.get(cursor, []) if in_month else []
            week_cells.append({
                "date": cursor,
                "in_month": in_month,
                "is_today": cursor == today_local,
                "entries": day_entries[:MONTH_CHIP_CAP],
                "overflow": max(0, len(day_entries) - MONTH_CHIP_CAP),
            })
            cursor += timedelta(days=1)
        month_grid.append(week_cells)

    month_dow_labels = [name[:3] for name in weekday_names]

    return templates.TemplateResponse(
        request,
        "upcoming.html",
        {
            "entries": entries,
            "week_grid": week_grid,
            "week_offset": week_offset,
            "week_start": week_start,
            "week_end_inclusive": week_end - timedelta(days=1),
            "month_grid": month_grid,
            "month_offset": month_offset,
            "month_start": month_start,
            "month_dow_labels": month_dow_labels,
            "date_filter": date_filter,
            "day_entries": date_filter_entries,
        },
    )


COMMON_TIMEZONES = [
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Stockholm",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Sao_Paulo", "Asia/Tokyo", "Asia/Seoul", "Asia/Shanghai",
    "Asia/Singapore", "Asia/Kolkata", "Australia/Sydney", "Pacific/Auckland",
    "UTC",
]


def next_episode_filler_info(progress, filler_map: dict[int, str]) -> tuple[int, str | None, int | None]:
    """Pure logic for issue #301's queue-card badge + "skip to next canon" action --
    kept separate from the /queue route so it's unit-testable without a database.
    `progress` is the anime's current library_entries.progress (None/0 both mean
    "nothing watched yet"). `filler_map` is {episode_number: status} sourced from
    #299's filler_episode_cache for one anime -- absence of a key means "unknown",
    never "canon" (see this repo's CLAUDE.md note on that table).

    Returns (next_episode_number, next_episode_status, skip_target):
    - `next_episode_status` is None when #299 has no cached row for the very next
      unwatched episode -- the badge must stay silent in that case (unknown != canon).
    - `skip_target` is the new progress value the "skip to next canon" action would
      apply, or None when there's no filler run to skip (next episode isn't filler,
      or its status is unknown). It walks forward from the next unwatched episode
      through consecutive cached 'filler' episodes and stops at the first episode
      that's either canon/mixed *or* has no cached row at all -- an uncached episode
      stops the walk rather than being assumed to still be filler, since only what
      AniFillerPedia has actually been researched should ever be trusted here.
    """
    next_ep = (progress or 0) + 1
    next_status = filler_map.get(next_ep)

    if next_status != "filler":
        return next_ep, next_status, None

    ep = next_ep
    while filler_map.get(ep) == "filler":
        ep += 1
    # `ep` is the first episode past the contiguous filler run -- either a cached
    # canon/mixed episode, or an uncached (unknown) one. Either way the run stops
    # here, so the new progress value is the episode right before it.
    return next_ep, next_status, ep - 1


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request, status: str = None):
    user, denied = _require_user(request)
    if denied:
        return denied

    queue_statuses = ["ALL", "PLANNING", "PAUSED"]
    active_status = status.upper() if status and status.upper() in queue_statuses else "ALL"

    status_filter = (
        "le.status IN ('PLANNING', 'PAUSED')"
        if active_status == "ALL"
        else "le.status = %s"
    )
    params = (user["id"],) if active_status == "ALL" else (active_status, user["id"])

    rows = db.fetchall(
        f"""
        SELECT
            a.id,
            a.title_english,
            a.title_romaji,
            a.cover_image_url,
            a.format,
            a.episodes,
            a.genres,
            a.external_links,
            a.average_score,
            le.status,
            le.progress,
            le.repeat_count,
            pn.watch_next_priority,
            pn.personal_tags,
            pn.mood_tags,
            pn.notes,
            rs.score   AS rec_score,
            rs.reason  AS rec_reason
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        LEFT JOIN personal_notes pn ON pn.anime_id = a.id AND pn.user_id = le.user_id
        LEFT JOIN recommendation_scores rs
               ON rs.anime_id = a.id AND rs.dismissed = false AND rs.user_id = le.user_id
        WHERE {status_filter} AND le.user_id = %s
        ORDER BY
            CASE WHEN pn.watch_next_priority IS NOT NULL THEN 0 ELSE 1 END,
            pn.watch_next_priority ASC NULLS LAST,
            rs.score DESC NULLS LAST,
            a.title_romaji
        """,
        params,
    )

    # Issue #301 -- filler-status badge + "skip to next canon" action, read-only
    # against #299's filler_episode_cache. Batched across every anime on this page
    # rather than one query per card. Absence of a (anime_id, episode_number) row
    # means "unknown", never "canon" -- see next_episode_filler_info() above.
    anime_ids = [row["id"] for row in rows]
    filler_rows = (
        db.fetchall(
            "SELECT anime_id, episode_number, status FROM filler_episode_cache WHERE anime_id = ANY(%s)",
            (anime_ids,),
        )
        if anime_ids
        else []
    )
    filler_by_anime: dict[int, dict[int, str]] = {}
    for fr in filler_rows:
        filler_by_anime.setdefault(fr["anime_id"], {})[fr["episode_number"]] = fr["status"]

    entries = []
    for row in rows:
        entry = dict(row)
        entry["streaming_links"] = [
            lnk for lnk in (row["external_links"] or [])
            if lnk.get("site") in STREAMING_SITES
        ]
        reason = row["rec_reason"] or {}
        matched = (reason.get("matched_genres") or [])[:4]
        if reason.get("matched_studio"):
            matched.append(reason["matched_studio"])
        entry["matched"] = matched

        next_ep, next_ep_status, skip_target = next_episode_filler_info(
            row["progress"], filler_by_anime.get(row["id"], {})
        )
        entry["next_episode_number"] = next_ep
        entry["next_episode_filler_status"] = next_ep_status
        entry["filler_skip_target"] = skip_target

        entries.append(entry)

    # Issue #191 -- rewatch reminder section, independent of the PLANNING/PAUSED
    # tabs above (it's driven by COMPLETED entries, a different slice of the
    # library entirely). See rewatch_due() for the trigger-logic decision.
    completed_rows = db.fetchall(
        """
        SELECT
            a.id,
            a.title_english,
            a.title_romaji,
            a.cover_image_url,
            a.format,
            a.episodes,
            a.genres,
            a.external_links,
            a.average_score,
            le.repeat_count,
            le.finish_date,
            pn.personal_tags,
            pn.mood_tags
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        LEFT JOIN personal_notes pn ON pn.anime_id = a.id AND pn.user_id = le.user_id
        WHERE le.status = 'COMPLETED' AND le.user_id = %s AND le.finish_date IS NOT NULL
        """,
        (user["id"],),
    )

    today = date.today()
    rewatch_entries = []
    for row in completed_rows:
        if not rewatch_due(row["finish_date"], today=today):
            continue
        entry = dict(row)
        entry["months_since"] = (today - row["finish_date"]).days // 30
        entry["streaming_links"] = [
            lnk for lnk in (row["external_links"] or [])
            if lnk.get("site") in STREAMING_SITES
        ]
        rewatch_entries.append(entry)

    # Most overdue (oldest finish_date) first.
    rewatch_entries.sort(key=lambda e: e["finish_date"])

    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "entries": entries,
            "queue_statuses": queue_statuses,
            "active_status": active_status,
            "rewatch_entries": rewatch_entries,
            "rewatch_reminder_months": REWATCH_REMINDER_MONTHS,
        },
    )


# ── Streaming Coverage (issue #182) ─────────────────────────────────────────────
# V2 marginal-value framing chosen over a raw per-service % (see issue #22's
# brainstorm, idea 3): for each service the user does NOT already own, how many
# additional Watching/Planning episodes-remaining would become newly covered by
# adding it. An entry already reachable via a service the user owns doesn't count
# toward any other service's marginal total — it's already unlocked, so adding a
# second service that also carries it wouldn't unlock anything new.
#
# Weighted by list status (issue #22 idea 4, carried into #182 as a hard
# requirement): only WATCHING/PLANNING drive the marginal-value ranking.
# COMPLETED/DROPPED entries are tallied separately as "historical footprint" —
# informational only (which services carried what you've already watched/dropped),
# never blended into the marginal-value numbers.
_STREAMING_ACTIVE_STATUSES = {"WATCHING", "PLANNING"}


def _episodes_remaining(progress, total_episodes) -> int:
    """total_episodes is null for anything AniList doesn't have a final episode
    count for yet (RELEASING with an unannounced order, e.g. One Piece). Treat that
    case as "at least 1 episode at stake" rather than 0 — an ongoing show with an
    unknown remaining count still represents real marginal value, and silently
    scoring it 0 would make every currently-airing un-owned-service title
    invisible to this ranking."""
    progress = progress or 0
    if total_episodes:
        return max(total_episodes - progress, 0)
    return 1


def _compute_streaming_coverage(user_id: int) -> dict:
    """Shared by GET /streaming and GET /stats (small summary card) — see
    _export_user_library for the same one-function/two-callers precedent (#90)."""
    owned = {
        r["service"] for r in db.fetchall(
            "SELECT service FROM user_streaming_services WHERE user_id = %s", (user_id,)
        )
    }

    rows = db.fetchall(
        """
        SELECT le.status, le.progress, a.episodes, a.external_links
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        WHERE le.user_id = %s AND le.status IN ('WATCHING', 'PLANNING', 'COMPLETED', 'DROPPED')
        """,
        (user_id,),
    )

    marginal: dict[str, dict] = {}
    footprint: dict[str, dict] = {}
    total_backlog_remaining = 0
    already_covered_remaining = 0

    for row in rows:
        sites = {
            lnk.get("site") for lnk in (row["external_links"] or [])
            if lnk.get("site") in STREAMING_SITES
        }

        if row["status"] in _STREAMING_ACTIVE_STATUSES:
            remaining = _episodes_remaining(row["progress"], row["episodes"])
            total_backlog_remaining += remaining
            if sites & owned:
                already_covered_remaining += remaining
            else:
                for site in sites - owned:
                    bucket = marginal.setdefault(site, {"episodes": 0, "titles": 0})
                    bucket["episodes"] += remaining
                    bucket["titles"] += 1
        else:
            # Historical footprint (COMPLETED/DROPPED) — title counts only, on every
            # site the title is available on (owned or not). Not weighted by
            # episodes-remaining (a completed show has ~0 remaining, which would
            # make this section trivially empty) and never merged into `marginal`.
            for site in sites:
                bucket = footprint.setdefault(site, {"titles": 0})
                bucket["titles"] += 1

    ranked = sorted(
        ({"service": s, **v} for s, v in marginal.items()),
        key=lambda x: (-x["episodes"], -x["titles"], x["service"]),
    )
    footprint_ranked = sorted(
        ({"service": s, **v} for s, v in footprint.items()),
        key=lambda x: (-x["titles"], x["service"]),
    )

    ts_row = db.fetchone(
        """
        SELECT MAX(a.last_synced_at) AS ts
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        WHERE le.user_id = %s
        """,
        (user_id,),
    )
    last_synced = ts_row["ts"].isoformat() if ts_row and ts_row["ts"] else None

    return {
        "owned": sorted(owned),
        "ranked": ranked,
        "footprint": footprint_ranked,
        "total_backlog_remaining": total_backlog_remaining,
        "already_covered_remaining": already_covered_remaining,
        "last_synced": last_synced,
    }


# ── Streaming Calendar (issue #228, v2 of #182) ──────────────────────────────────
# "Only pay for months your shows air" — a 12-month subscribe/cancel calendar built
# entirely from airing_schedule_cache (future episode air dates) + anime.external_links,
# no new AniList calls or schema. Spun out of #22's brainstorm (idea 7, "cancel
# candidates", and idea 13, "simulcast urgency"); the actual month-bucketing reuses
# #182's site-crediting rule (see _credit_streaming_sites) rather than inventing a new
# one, so the two features stay conceptually consistent even though the calendar is a
# separate read-model from _compute_streaming_coverage.
_CALENDAR_HORIZON_MONTHS = 12


def _add_months(d: date, n: int) -> date:
    """First-of-month date `n` months after the first of `d`'s month."""
    total = d.month - 1 + n
    year = d.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def _credit_streaming_sites(sites: set, owned: set) -> set:
    """Same site-crediting rule _compute_streaming_coverage uses for its
    marginal/already-covered split, factored out so the calendar can apply it
    per-month: a title already reachable via an owned service credits every owned
    service that carries it (the "cancel" side of the calendar — deliberately no
    set-cover optimization across owned services, same simplification #22 and #182
    both settled on, see #22's Out of scope); otherwise it credits every un-owned
    service that carries it (the "subscribe" side, matching the marginal-value
    ranking's sites - owned)."""
    owned_hit = sites & owned
    return owned_hit if owned_hit else sites


def _compute_streaming_calendar(user_id: int) -> dict:
    """12-month subscribe/cancel calendar for this user's WATCHING/PLANNING library.

    For each service touched by that library, computes which of the next 12 UTC
    calendar months (starting this month) have at least one upcoming episode airing
    from a title credited to that service via _credit_streaming_sites.

    Two data-shape quirks of airing_schedule_cache drive deliberate handling here —
    see scripts/sync_airing_schedule.py:
      1. It only holds NOT-yet-aired episodes for anime.status = 'RELEASING', and is
         deleted+reinserted on every hourly refresh — an episode that already aired
         this month is gone from the cache the moment it airs. Left alone, that would
         make the *current* month look emptier as the month goes on even for a show
         that's actively mid-season right now. Fix: a RELEASING title whose EARLIEST
         still-scheduled episode lands in the current or next calendar month also
         credits the *current* month — "next episode is imminent" stands in for "this
         show is airing right now." Deliberately bounded to a one-month lookahead
         rather than "has any future row at all": a RELEASING title whose next known
         episode is, say, 11 months out (an irregular/long-hiatus schedule) is not
         "airing right now" in any useful subscribe/cancel sense, and crediting the
         current month for it would be actively misleading.
      2. A WATCHING/PLANNING title with NO airing_schedule_cache rows at all is either
         between cours (RELEASING with a gap the cache hasn't caught) or an announced
         sequel with no confirmed date yet (NOT_YET_RELEASED). Per issue #228's open
         question, these go in a separate `tba_titles` bucket per credited service
         rather than being silently omitted — "don't know when" isn't "not needed."
         A title that DOES have a known future episode, just one that falls past the
         12-month horizon, is not TBA (its date isn't unknown) — it's simply absent
         from every visible month, same as a fully-aired backlog title below.
      A fully-aired backlog title (status FINISHED, nothing left airing) has no next
      air date to be TBA about either — it's just deliberately absent from the
      calendar, since this feature is about air-date-driven urgency, not "when should
      I get around to my backlog," which the existing marginal-value ranking already
      covers on this same page.
    """
    owned = {
        r["service"] for r in db.fetchall(
            "SELECT service FROM user_streaming_services WHERE user_id = %s", (user_id,)
        )
    }

    today = datetime.now(timezone.utc).date()
    month_starts = [_add_months(date(today.year, today.month, 1), i) for i in range(_CALENDAR_HORIZON_MONTHS)]
    horizon_end = _add_months(month_starts[0], _CALENDAR_HORIZON_MONTHS)

    entries = db.fetchall(
        """
        SELECT le.anime_id, a.title_english, a.title_romaji, a.status, a.external_links
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        WHERE le.user_id = %s AND le.status IN ('WATCHING', 'PLANNING')
        """,
        (user_id,),
    )
    if not entries:
        return {"months": [d.isoformat() for d in month_starts], "services": []}

    anime_ids = [e["anime_id"] for e in entries]
    schedule_rows = db.fetchall(
        "SELECT anime_id, airing_at FROM airing_schedule_cache WHERE anime_id = ANY(%s)",
        (anime_ids,),
    )
    schedule_by_anime: dict[int, list] = {}
    for r in schedule_rows:
        schedule_by_anime.setdefault(r["anime_id"], []).append(r["airing_at"])

    # matrix[service][month_index] -> {"episodes": int, "anime_ids": set}
    matrix: dict[str, list[dict]] = {}
    tba: dict[str, set] = {}

    def _bucket(service: str) -> list[dict]:
        if service not in matrix:
            matrix[service] = [{"episodes": 0, "anime_ids": set()} for _ in range(_CALENDAR_HORIZON_MONTHS)]
        return matrix[service]

    for e in entries:
        sites = {
            lnk.get("site") for lnk in (e["external_links"] or [])
            if lnk.get("site") in STREAMING_SITES
        }
        if not sites:
            continue

        credited = _credit_streaming_sites(sites, owned)
        title = e["title_english"] or e["title_romaji"]
        airings = schedule_by_anime.get(e["anime_id"], [])

        months_hit: set[int] = set()
        earliest_gap_months = None
        for airing_at in airings:
            airing_date = airing_at.date()
            gap = (airing_date.year - month_starts[0].year) * 12 + (airing_date.month - month_starts[0].month)
            if earliest_gap_months is None or gap < earliest_gap_months:
                earliest_gap_months = gap
            if 0 <= gap < _CALENDAR_HORIZON_MONTHS:
                months_hit.add(gap)

        if e["status"] == "RELEASING" and earliest_gap_months is not None and earliest_gap_months <= 1:
            months_hit.add(0)  # next episode imminent — see docstring quirk 1

        if not months_hit:
            # `airings` empty -> genuinely unknown next date (quirk 2). `airings`
            # non-empty but every date beyond the horizon -> known, just not shown;
            # neither case is "needed" this window, but only the first is TBA.
            if not airings and e["status"] in ("RELEASING", "NOT_YET_RELEASED"):
                for service in credited:
                    tba.setdefault(service, set()).add(title)
            continue

        for mi in months_hit:
            for service in credited:
                b = _bucket(service)[mi]
                b["episodes"] += 1
                b["anime_ids"].add(e["anime_id"])

    services = []
    for service in set(matrix) | set(tba):
        months = matrix.get(service) or [{"episodes": 0, "anime_ids": set()} for _ in range(_CALENDAR_HORIZON_MONTHS)]
        total_episodes = sum(m["episodes"] for m in months)
        services.append({
            "service": service,
            "owned": service in owned,
            "months": [
                {"needed": m["episodes"] > 0, "episodes": m["episodes"], "titles": len(m["anime_ids"])}
                for m in months
            ],
            "tba_titles": sorted(tba.get(service, set())),
            "total_episodes": total_episodes,
        })

    services.sort(key=lambda s: (not s["owned"], -s["total_episodes"], s["service"]))

    return {
        "months": [d.isoformat() for d in month_starts],
        "services": services,
    }


# ── Streaming Set-Cover Recommendation (issue #255, v3 of #182/#228) ────────────
# The actual "which combination of services do I need" answer #22's original
# brainstorm deferred (idea 11, "set-cover framing") — title-level, not
# episodes-remaining-weighted, and framed as combinations of services rather than
# each service scored in isolation like #182's marginal-value ranking above (kept
# unchanged; this is an additive layer, not a replacement — see #255's Out of scope).
#
# Coverage universe = Watching + Planning + "Upcoming", Completed excluded (#255's
# own scope, explicitly matching #182's precedent that a finished title needs no
# further "access"). "Upcoming" reuses #228's airing_schedule_cache rather than a
# second upcoming-episode source: any tracked title that ISN'T already
# Watching/Planning but still has an unaired episode on the calendar (in practice,
# almost always a Paused/Repeating/Dropped title whose next episode is still
# airing) — a title genuinely still relevant to a subscribe/cancel decision even
# though its list status alone wouldn't have surfaced it. A title with no
# airing_schedule_cache rows at all and a non-Watching/Planning status (an
# unannounced/finished backlog title) is correctly left out.
def _streaming_universe(user_id: int) -> list[dict]:
    rows = db.fetchall(
        """
        SELECT le.anime_id AS id, a.title_english, a.title_romaji, a.external_links,
               le.status, a.episodes, le.progress, a.genres
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        WHERE le.user_id = %s
          AND le.status <> 'COMPLETED'
          AND (
                le.status IN ('WATCHING', 'PLANNING')
                OR EXISTS (
                    SELECT 1 FROM airing_schedule_cache asc2
                    WHERE asc2.anime_id = le.anime_id AND asc2.airing_at > now()
                )
              )
        """,
        (user_id,),
    )
    universe = []
    for row in rows:
        sites = {
            lnk.get("site") for lnk in (row["external_links"] or [])
            if lnk.get("site") in STREAMING_SITES
        }
        universe.append({
            "id": row["id"],
            "title": row["title_english"] or row["title_romaji"],
            "sites": sites,
            "status": row["status"],
            # #285: episodes-remaining weight, same fallback rule as
            # _compute_streaming_coverage's marginal-value ranking (see
            # _episodes_remaining's docstring) — reused here rather than
            # reinvented so the two read-models handle an unknown
            # anime.episodes total identically.
            "remaining": _episodes_remaining(row["progress"], row["episodes"]),
            # #286: carried through so _compute_streaming_genre_affinity can
            # restrict this same universe per-genre without a second query.
            "genres": row["genres"] or [],
        })
    return universe


def _greedy_set_cover(services: list[str], universe_ids: set, id_to_sites: dict) -> list[str]:
    """Standard greedy set-cover approximation: repeatedly pick the service that
    covers the most still-uncovered ids, until nothing is left uncovered (or no
    remaining candidate covers anything further). Exact optimal set-cover is
    NP-hard and unnecessary at this scale (a handful of real streaming services)
    — #255's own "Open questions" section explicitly leaves this call to the
    implementer.

    Tie-break rule: alphabetical by service name. `services` is iterated in
    already-sorted order and a later candidate only replaces the current best on a
    STRICT `>` (not `>=`), so among two services covering an equal number of
    still-uncovered titles the alphabetically-first one is kept — simple,
    deterministic, and doesn't require a secondary metric this feature has no
    strong opinion on (e.g. price, which AniDex has no data for at all)."""
    remaining = set(universe_ids)
    selected: list[str] = []
    candidates = sorted(services)
    while remaining:
        best_service, best_covers = None, set()
        for svc in candidates:
            if svc in selected:
                continue
            covers = {aid for aid in remaining if svc in id_to_sites.get(aid, ())}
            if len(covers) > len(best_covers):
                best_service, best_covers = svc, covers
        if not best_service or not best_covers:
            break  # nothing left can be covered by any candidate — shouldn't happen
                   # since `universe_ids` is pre-filtered to owned-coverable titles
        selected.append(best_service)
        remaining -= best_covers
    return selected


def _title_pairs(ids, id_to_title: dict, id_to_status: dict) -> list[dict]:
    """{id, title, status} pairs, title-sorted — shared shape for every
    title-chip list `_compute_streaming_setcover` returns (#271: threads the
    anime id through so the template can link each chip to
    /anime/{id}/notes, instead of the bare title strings it used to return)."""
    return sorted(
        (
            {"id": aid, "title": id_to_title[aid], "status": id_to_status[aid]}
            for aid in ids
        ),
        key=lambda t: t["title"],
    )


def _compute_streaming_setcover(user_id: int) -> dict:
    """Shared by GET /streaming. A distinct read-model from _compute_streaming_coverage
    above (title-level and combination-framed, not episodes-remaining/per-service) but
    built over the same universe/STREAMING_SITES machinery."""
    owned = {
        r["service"] for r in db.fetchall(
            "SELECT service FROM user_streaming_services WHERE user_id = %s", (user_id,)
        )
    }

    universe = _streaming_universe(user_id)
    covered = [t for t in universe if t["sites"]]
    # Titles on no recognized streaming service at all — never silently dropped
    # from the count (#255 acceptance criteria: an explicit, honest bucket).
    # {id, title} pairs (not bare strings, #271) so the template can link each
    # chip through to that title's real /anime/{id}/notes page.
    uncovered_titles = sorted(
        (
            {"id": t["id"], "title": t["title"], "status": t["status"]}
            for t in universe if not t["sites"]
        ),
        key=lambda t: t["title"],
    )

    id_to_sites = {t["id"]: t["sites"] for t in covered}
    id_to_title = {t["id"]: t["title"] for t in covered}
    id_to_status = {t["id"]: t["status"] for t in covered}
    # #285: episodes-remaining weight per title, carried over from
    # _streaming_universe (which already applies the NULL-episodes fallback via
    # _episodes_remaining). Title count stays available too (`count` fields
    # below) as a secondary detail — episodes becomes the headline metric.
    id_to_remaining = {t["id"]: t["remaining"] for t in covered}

    owned_coverable_ids = {t["id"] for t in covered if t["sites"] & owned}
    owned_total_covered = len(owned_coverable_ids)
    owned_total_covered_episodes = sum(id_to_remaining[aid] for aid in owned_coverable_ids)
    # #270: denominator for the at-a-glance summary's coverage % — every episode
    # remaining across the WHOLE universe (covered + uncovered_titles), not just
    # the covered subset, since a title on no recognized service at all still
    # counts against "how much of your list is covered." Reuses `universe`
    # (already fetched above), no new query.
    total_universe_episodes = sum(t["remaining"] for t in universe)

    # #285 out of scope: the set-cover algorithm's shape is unchanged — it still
    # selects the minimal combination by title coverage, not episode weight.
    minimal_combination = (
        _greedy_set_cover(sorted(owned), owned_coverable_ids, id_to_sites) if owned else []
    )

    # Per-owned-service marginal/unique contribution: the title list only
    # reachable through that ONE owned service (what you'd lose by dropping it) —
    # independent of which services the greedy algorithm above actually selected,
    # since #255 asks for this "for each currently-owned service", not just the
    # minimal subset.
    marginal = []
    for svc in sorted(owned):
        unique_ids = [
            aid for aid in owned_coverable_ids if id_to_sites[aid] & owned == {svc}
        ]
        marginal.append({
            "service": svc,
            "count": len(unique_ids),
            "episodes": sum(id_to_remaining[aid] for aid in unique_ids),
            "titles": _title_pairs(unique_ids, id_to_title, id_to_status),
        })

    # Full ranked list of every allowlisted service, owned or not — no coverage
    # threshold cutoff (#255 acceptance criteria: transparency over curation).
    total_covered_episodes = sum(id_to_remaining.values())
    ranked_all = []
    for svc in sorted(STREAMING_SITES):
        ids = [t["id"] for t in covered if svc in t["sites"]]
        episodes = sum(id_to_remaining[aid] for aid in ids)
        ranked_all.append({
            "service": svc,
            "owned": svc in owned,
            "count": len(ids),
            "pct": round(len(ids) / len(covered) * 100, 1) if covered else 0.0,
            "episodes": episodes,
            "episodes_pct": round(episodes / total_covered_episodes * 100, 1) if total_covered_episodes else 0.0,
            "titles": _title_pairs(ids, id_to_title, id_to_status),
        })
    # #285: episodes-remaining is now the headline sort key, title count the tie-break.
    ranked_all.sort(key=lambda r: (-r["episodes"], -r["count"], r["service"]))

    # Swap/consolidation suggestion: an un-owned service whose total coverage
    # alone matches or beats the current owned combination's total coverage —
    # driven directly by ranked_all above, per #255's own scope note ("not a
    # separate computation"). Guarded on `owned` being non-empty: with nothing
    # owned there's no existing combination to "swap out" of, just a plain
    # recommendation the full ranked list below already covers; and on
    # episodes > 0 so two services that both cover nothing don't produce a
    # meaningless 0-beats-0 suggestion. #285: compared on episodes-remaining
    # now, not raw title count.
    swap_candidates = []
    if owned:
        swap_candidates = sorted(
            (
                r for r in ranked_all
                if not r["owned"] and r["episodes"] > 0 and r["episodes"] >= owned_total_covered_episodes
            ),
            key=lambda r: (-r["episodes"], -r["count"], r["service"]),
        )

    return {
        "universe_size": len(universe),
        "uncovered_titles": uncovered_titles,
        "owned": sorted(owned),
        "owned_total_covered": owned_total_covered,
        "owned_total_covered_episodes": owned_total_covered_episodes,
        "total_universe_episodes": total_universe_episodes,
        "minimal_combination": minimal_combination,
        "marginal": marginal,
        "ranked_all": ranked_all,
        "swap_candidates": swap_candidates,
    }


# ── Cancel candidates (issue #284, inverse framing of #255) ─────────────────────
# #255's `marginal` above already computes "what each owned service uniquely
# covers" (a subscribe-value framing) over the broader Upcoming-inclusive
# set-cover universe. #284 asks a related but distinct question worth its own
# read-model: "if I cancel this service I already pay for, what % of my actual
# Watching/Planning list — not the wider set-cover universe — becomes
# uncovered?" Denominator is every WATCHING/PLANNING title regardless of
# whether it's on any recognized streaming service at all (same honesty
# principle as _compute_streaming_setcover's uncovered_titles bucket — a title
# with zero links still counts against the total, it just never contributes to
# any single service's uncovered count either). Reuses
# _STREAMING_ACTIVE_STATUSES (the same WATCHING/PLANNING definition
# _compute_streaming_coverage's episode-weighted ranking already uses) and the
# STREAMING_SITES allowlist — no new query shape, no new AniList calls.
def _compute_streaming_cancel_candidates(user_id: int) -> dict:
    owned = {
        r["service"] for r in db.fetchall(
            "SELECT service FROM user_streaming_services WHERE user_id = %s", (user_id,)
        )
    }

    rows = db.fetchall(
        """
        SELECT le.anime_id AS id, a.title_english, a.title_romaji, le.status,
               a.external_links, a.episodes, le.progress
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        WHERE le.user_id = %s AND le.status = ANY(%s)
        """,
        (user_id, sorted(_STREAMING_ACTIVE_STATUSES)),
    )
    total_titles = len(rows)

    id_to_title = {r["id"]: (r["title_english"] or r["title_romaji"]) for r in rows}
    id_to_status = {r["id"]: r["status"] for r in rows}
    id_to_sites = {
        r["id"]: {
            lnk.get("site") for lnk in (r["external_links"] or [])
            if lnk.get("site") in STREAMING_SITES
        }
        for r in rows
    }
    # #285: episodes-remaining weight per title — same fallback rule as
    # _compute_streaming_setcover / _compute_streaming_coverage (NULL
    # anime.episodes, e.g. a still-airing title with no confirmed final count,
    # is treated as "at least 1 episode at stake" by _episodes_remaining rather
    # than 0, so it stays visible instead of silently vanishing from a cancel
    # service's impact).
    id_to_remaining = {
        r["id"]: _episodes_remaining(r["progress"], r["episodes"]) for r in rows
    }
    total_episodes_remaining = sum(id_to_remaining.values())

    # Per-owned-service: titles reachable ONLY through that one owned service
    # among the owned set — exactly what becomes uncovered if it were cancelled
    # (a title also reachable via another owned service stays covered either
    # way, so it's not part of this service's cancel impact).
    candidates = []
    for svc in sorted(owned):
        unique_ids = [
            aid for aid, sites in id_to_sites.items() if sites & owned == {svc}
        ]
        count = len(unique_ids)
        episodes = sum(id_to_remaining[aid] for aid in unique_ids)
        candidates.append({
            "service": svc,
            "count": count,
            "pct": round(count / total_titles * 100, 1) if total_titles else 0.0,
            "episodes": episodes,
            "episodes_pct": round(episodes / total_episodes_remaining * 100, 1) if total_episodes_remaining else 0.0,
            "titles": _title_pairs(unique_ids, id_to_title, id_to_status),
            # #285: redundancy is now judged on episode impact, not raw title
            # count — a uniquely-covered title that happens to have 0 episodes
            # actually remaining (progress caught up to a known total, just not
            # yet marked Completed) truly costs nothing to cancel. In practice
            # this only differs from `count == 0` in that edge case; every
            # existing title still contributes >=1 whenever it has any episodes
            # left or an unknown total (see _episodes_remaining).
            "fully_redundant": episodes == 0,
        })

    # Safest-to-cancel first: lowest impact surfaces at the top of the list.
    # #285: episodes-remaining is now the headline sort key, title count the
    # tie-break (was title count only).
    candidates.sort(key=lambda c: (c["episodes"], c["count"], c["service"]))

    return {
        "total_titles": total_titles,
        "total_episodes_remaining": total_episodes_remaining,
        "candidates": candidates,
    }


# ── Genre affinity coverage (issue #286) ─────────────────────────────────────
# #286's own Scope section required verifying `anime.genres` actually supports a
# meaningful per-genre breakdown before committing to building this. Verification
# (2026-08-22, against prod): `genres` is AniList's own fixed genre taxonomy — a
# DISTINCT scan of every value on prod returned exactly 18 canonical values
# (Action, Adventure, Comedy, Drama, Ecchi, Fantasy, Horror, Mahou Shoujo, Mecha,
# Music, Mystery, Psychological, Romance, Sci-Fi, Slice of Life, Sports,
# Supernatural, Thriller) with zero casing/format drift — Title Case throughout,
# no near-duplicates. Populated on 1286/1288 anime rows (99.8%), averaging ~3-4
# genres per title. It's coarse rather than micro-granular (no "Isekai"/"Shounen"
# splits — those live in `tags`, not `genres`), but that's the exact same
# coarseness /stats' existing "top_genre" and taste-drift chart already treat as
# a meaningful signal off this identical column — not a new bar. Confirmed
# viable; proceeding to step 2 rather than closing as not-viable.
#
# "Top genre" here means top-RATED (what #286 actually asks to correlate against
# library_entries.score), not top-watched (that's /stats' existing top_genre,
# ranked by frequency) — average score per genre across COMPLETED, scored
# entries only, gated by the same MIN_TITLES-before-a-genre-counts-as-signal
# rule _compute_studio_loyalty (#223, STUDIO_LOYALTY_MIN_TITLES) already
# established for an identical one-off-title-isn't-a-signal tradeoff. Not a
# reference to that constant directly — it's defined later in this file (near
# /stats) than this streaming-cluster code, so importing here would hit a
# NameError at module load; same value (3), same reasoning, kept as its own
# constant instead.
GENRE_AFFINITY_MIN_TITLES = 3


def _compute_streaming_genre_affinity(user_id: int) -> dict | None:
    """Correlates episode-weighted coverage (#285) against a user's top-RATED
    genres for the /streaming page (#286). Returns None when there's no scored
    COMPLETED genre clearing the MIN_TITLES gate yet — same "hide the section
    entirely" pattern _compute_studio_loyalty/_compute_drop_patterns use, rather
    than rendering an empty/noisy chart."""
    genre_rows = db.fetchall(
        """
        SELECT
            genre_elem AS genre,
            COUNT(*) AS title_count,
            COUNT(*) FILTER (WHERE le.score IS NOT NULL AND le.score > 0) AS scored_count,
            AVG(le.score) FILTER (WHERE le.score IS NOT NULL AND le.score > 0) AS avg_score
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id,
             jsonb_array_elements_text(a.genres) AS genre_elem
        WHERE le.user_id = %s AND le.status = 'COMPLETED'
        GROUP BY genre_elem
        """,
        (user_id,),
    )
    if not genre_rows:
        return None

    qualifying = []
    excluded_low_volume = 0
    excluded_unscored = 0
    for row in genre_rows:
        title_count = int(row["title_count"])
        scored_count = int(row["scored_count"])
        if title_count < GENRE_AFFINITY_MIN_TITLES:
            excluded_low_volume += 1
            continue
        if scored_count == 0:
            excluded_unscored += 1
            continue
        qualifying.append({
            "genre": row["genre"],
            "title_count": title_count,
            "scored_count": scored_count,
            "avg_score": round(float(row["avg_score"]), 2),
        })

    if not qualifying:
        return None

    qualifying.sort(key=lambda g: (-g["avg_score"], -g["title_count"], g["genre"]))

    # Coverage side: the same Watching/Planning/Upcoming universe #255/#284/#285
    # already use (via _streaming_universe, which now also carries a.genres —
    # see #286's edit to that function), restricted per-genre and
    # episode-weighted the same way _compute_streaming_setcover's ranked_all is.
    # A genre that clears the rating-side gate but has nothing left in that
    # universe (e.g. every title carrying it is already Completed) has no
    # coverage question left to answer, so it's dropped here too rather than
    # shown with an empty services list.
    owned = {
        r["service"] for r in db.fetchall(
            "SELECT service FROM user_streaming_services WHERE user_id = %s", (user_id,)
        )
    }
    universe = _streaming_universe(user_id)

    genres_out = []
    for g in qualifying:
        genre_titles = [t for t in universe if g["genre"] in t["genres"]]
        total_episodes = sum(t["remaining"] for t in genre_titles)
        if total_episodes == 0:
            continue

        services_out = []
        for svc in sorted(STREAMING_SITES):
            episodes = sum(t["remaining"] for t in genre_titles if svc in t["sites"])
            if episodes == 0:
                continue
            services_out.append({
                "service": svc,
                "owned": svc in owned,
                "episodes": episodes,
                "pct": round(episodes / total_episodes * 100, 1),
            })
        services_out.sort(key=lambda s: (-s["episodes"], s["service"]))

        genres_out.append({
            "genre": g["genre"],
            "avg_score": g["avg_score"],
            "title_count": g["title_count"],
            "universe_title_count": len(genre_titles),
            "total_episodes": total_episodes,
            "services": services_out,
        })

    if not genres_out:
        return None

    return {
        "genres": genres_out,
        "min_titles": GENRE_AFFINITY_MIN_TITLES,
        "total_genres": len(genre_rows),
        "excluded_low_volume": excluded_low_volume,
        "excluded_unscored": excluded_unscored,
    }


# ── At-a-glance summary (issue #270) ─────────────────────────────────────────
# Synthesizes /streaming's top-of-page summary card from data _compute_streaming_
# setcover() and _compute_streaming_cancel_candidates() already computed for their
# own sections below — no new queries, just composition. Returns numeric/string
# fields for the template to assemble into 1-3 sentences via its own i18n keys
# (not a pre-formatted English string here) so each clause stays independently
# localizable and can be omitted without leaving a broken/empty fragment behind.
#
# Three independent clauses, each degrading on its own:
#   1. Coverage: "no owned services" (has_owned False) and "empty universe"
#      (total_episodes 0, avoids a division by zero) both take priority over the
#      normal X%-of-list sentence; `fully_covered` swaps in a distinct "already
#      covering everything" sentence instead of a literal "100%" (#270 acceptance
#      criterion).
#   2. Cancel: only set when the safest candidate is actually `fully_redundant`
#      — "safe to cancel" said about a service that isn't would be actively
#      wrong, not just unhelpful.
#   3. Swap: reuses setcover's `swap_candidates` (its top entry = the best
#      un-owned alternative, per #270's own implementation guidance) but is
#      deliberately suppressed once `fully_covered` is True — "adding a service
#      would help" makes no sense once there's nothing left to add for, which is
#      exactly the already-100%-covered case #270's acceptance criteria calls
#      out by name.
def _compute_streaming_atglance(setcover: dict, cancel: dict) -> dict:
    owned = setcover["owned"]
    total_episodes = setcover["total_universe_episodes"]
    covered_episodes = setcover["owned_total_covered_episodes"]
    coverage_pct = round(covered_episodes / total_episodes * 100, 1) if total_episodes else 0.0
    fully_covered = total_episodes > 0 and covered_episodes >= total_episodes

    cancel_service = None
    top_cancel = cancel["candidates"][0] if cancel["candidates"] else None
    if top_cancel and top_cancel["fully_redundant"]:
        cancel_service = top_cancel["service"]

    swap_service = None
    swap_episodes = None
    if not fully_covered and setcover["swap_candidates"]:
        top_swap = setcover["swap_candidates"][0]
        swap_service = top_swap["service"]
        swap_episodes = top_swap["episodes"]

    return {
        "has_owned": bool(owned),
        "owned_count": len(owned),
        "total_episodes": total_episodes,
        "covered_episodes": covered_episodes,
        "coverage_pct": coverage_pct,
        "fully_covered": fully_covered,
        "cancel_service": cancel_service,
        "swap_service": swap_service,
        "swap_episodes": swap_episodes,
    }


@app.get("/streaming", response_class=HTMLResponse)
def streaming_page(request: Request):
    user, denied = _require_user(request)
    if denied:
        return denied

    coverage = _compute_streaming_coverage(user["id"])
    calendar = _compute_streaming_calendar(user["id"])
    setcover = _compute_streaming_setcover(user["id"])
    cancel = _compute_streaming_cancel_candidates(user["id"])
    genre_affinity = _compute_streaming_genre_affinity(user["id"])  # issue #286
    atglance = _compute_streaming_atglance(setcover, cancel)  # issue #270

    return templates.TemplateResponse(
        request,
        "streaming.html",
        {
            "owned_services": coverage["owned"],
            "footprint": coverage["footprint"],
            "last_synced": coverage["last_synced"],
            "calendar_months": calendar["months"],
            "calendar_services": calendar["services"],
            "sc_universe_size": setcover["universe_size"],
            "sc_uncovered_titles": setcover["uncovered_titles"],
            "sc_owned_total_covered": setcover["owned_total_covered"],
            "sc_owned_total_covered_episodes": setcover["owned_total_covered_episodes"],
            "sc_minimal_combination": setcover["minimal_combination"],
            "sc_marginal": setcover["marginal"],
            "sc_ranked_all": setcover["ranked_all"],
            "sc_swap_candidates": setcover["swap_candidates"],
            "cancel_total_titles": cancel["total_titles"],
            "cancel_total_episodes_remaining": cancel["total_episodes_remaining"],
            "cancel_candidates": cancel["candidates"],
            "genre_affinity": genre_affinity,
            "glance_has_owned": atglance["has_owned"],
            "glance_owned_count": atglance["owned_count"],
            "glance_total_episodes": atglance["total_episodes"],
            "glance_covered_episodes": atglance["covered_episodes"],
            "glance_coverage_pct": atglance["coverage_pct"],
            "glance_fully_covered": atglance["fully_covered"],
            "glance_cancel_service": atglance["cancel_service"],
            "glance_swap_service": atglance["swap_service"],
            "glance_swap_episodes": atglance["swap_episodes"],
        },
    )


def _credential_connection_status(configured: bool, service: str, user_id: int) -> str:
    """One of 'not_connected' / 'connected' / 'needs_attention' for a Settings →
    Credentials status pill (#188). Mirrors /api/sync/status's own lookup of the
    latest full_sync/force_full_resync row's steps[] — a credential that's set
    but whose most recent sync step for that service came back 'error' is shown
    as needing attention rather than a false-positive "Connected"."""
    if not configured:
        return "not_connected"
    row = db.fetchone(
        "SELECT steps FROM sync_log WHERE user_id = %s AND type IN ('full_sync', 'force_full_resync') "
        "ORDER BY run_at DESC LIMIT 1",
        (user_id,),
    )
    steps = (row["steps"] if row else None) or []
    step = next((s for s in steps if s.get("service") == service), None)
    if step and step.get("status") == "error":
        return "needs_attention"
    return "connected"


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, link_error: str = "", password_error: str = "", saved: str = "",
    csv_import_error: str = "", cr_override_error: str = "", twofa_error: str = "",
):
    user, denied = _require_user(request)
    if denied:
        return denied

    current = config.get_all(user["id"])

    row = db.fetchone(
        "SELECT MAX(synced_at) AS ts FROM library_entries WHERE user_id = %s", (user["id"],)
    )
    last_synced = row["ts"].isoformat() if row and row["ts"] else None

    # Issue #159 — manual Crunchyroll title/season -> AniList id overrides, listed
    # here for editing. Personal-layer table (cr_title_overrides); only the app
    # writes to it, scripts/sync_crunchyroll.py only ever reads it.
    cr_overrides = db.fetchall(
        "SELECT id, series_title, season_number, anilist_id FROM cr_title_overrides "
        "WHERE user_id = %s ORDER BY series_title, season_number",
        (user["id"],),
    )

    # Issue #82 — active sessions list. current_token identifies which row is
    # "this device" purely server-side (list_active_sessions never hands the raw
    # token back), so the current row can be flagged without ever putting a live
    # session token into the rendered page.
    active_sessions = sessions.list_active_sessions(user["id"], request.session.get("sid"))

    # Issue #207 — MCP server personal access tokens, "API Access" tab.
    pat_tokens = pat.list_tokens(user["id"])

    # Issue #182 — "services I own", edited from this tab, scored against the
    # library on the separate /streaming page.
    owned_streaming_services = {
        r["service"] for r in db.fetchall(
            "SELECT service FROM user_streaming_services WHERE user_id = %s", (user["id"],)
        )
    }

    # Issue #188 — status pill per credential card. "not_connected" purely from
    # whether the value is set; "needs_attention" layers in the most recent
    # full_sync/force_full_resync run's per-service step, the same steps[] the
    # sync-history table and step chips already read — so a card doesn't keep
    # showing "Connected" after a credential has visibly started failing sync.
    cred_status = {
        "anilist": _credential_connection_status(
            bool(current.get("anilist_username")) and bool(current.get("anilist_token")),
            "anilist_postgres", user["id"],
        ),
        "crunchyroll": _credential_connection_status(
            bool(current.get("cr_etp_rt")), "crunchyroll", user["id"],
        ),
        "netflix": _credential_connection_status(
            bool(current.get("netflix_cookie_header")) and bool(current.get("netflix_profile_guid")),
            "netflix", user["id"],
        ),
    }

    # Issue #204 — same GIT_SHA value the admin-only Operability tab already
    # surfaces as plain text (_instance_health's build_version), reused here as a
    # real link to the commit on GitHub so every user, not just admins, can see
    # exactly what's deployed.
    build_version = _build_version()

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": current,
            "cred_status": cred_status,
            "timezones": COMMON_TIMEZONES,
            "languages": i18n.SUPPORTED_LOCALES,
            "language_labels": i18n.LOCALE_LABELS,
            "last_synced": last_synced,
            "build_version": build_version,
            "build_commit_url": f"{GITHUB_REPO_URL}/commit/{build_version}" if build_version else None,
            "account": {
                "has_password": bool(user["password_hash"]),
                "google_linked": bool(user["google_id"]),
                "discord_linked": bool(user["discord_id"]),
                "is_admin": user["is_admin"],
                "totp_enabled": user["totp_enabled"],
            },
            "oauth_google_configured": oauth_configured("google"),
            "oauth_discord_configured": oauth_configured("discord"),
            "link_error": link_error,
            "password_error": password_error,
            "saved": saved,
            "csv_import_error": csv_import_error,
            "cr_override_error": cr_override_error,
            "twofa_error": twofa_error,
            "cr_overrides": cr_overrides,
            "active_sessions": active_sessions,
            "pat_tokens": pat_tokens,
            "all_streaming_services": sorted(STREAMING_SITES),
            "owned_streaming_services": owned_streaming_services,
            "privacy": {
                "hidden_tags": ", ".join(json.loads(config.get(user["id"], "hidden_tags") or "[]")),
                "anonymize_activity": config.get(user["id"], "anonymize_activity") == "true",
            },
        },
    )


@app.post("/settings/sessions/{session_id}/revoke")
def settings_revoke_session(request: Request, session_id: int):
    """Revoke one of the current user's own sessions (issue #82). sessions.revoke_session
    scopes the UPDATE to (id, user_id), so passing another user's session_id here just
    finds no matching row and no-ops — there's no separate ownership check needed above
    that. Revoking the session the caller is CURRENTLY using logs them out immediately,
    same as clicking Logout; revoking any other one just removes it from the list.

    Only redirects with the success message (saved=session_revoked) when a row was
    actually revoked — revoke_session() returns None on a no-op (already revoked, or
    a stale/tampered id), e.g. a double-click or the same session revoked from two
    open tabs, and that must NOT show a false "revoked" confirmation.

    No explicit ?tab= on either redirect, matching every other saved=/no-op redirect
    in this file — script.js's savedTabMap (session_revoked -> account) resolves the
    tab client-side, and account is also just the default first tab either way.
    """
    user, denied = _require_user(request)
    if denied:
        return denied

    revoked_token_hash = sessions.revoke_session(session_id, user["id"])
    current_sid = request.session.get("sid")
    if revoked_token_hash and current_sid and revoked_token_hash == sessions.hash_token(current_sid):
        request.session.clear()
        return RedirectResponse(url="/auth/login", status_code=303)
    if revoked_token_hash:
        return RedirectResponse(url="/settings?saved=session_revoked", status_code=303)
    return RedirectResponse(url="/settings", status_code=303)


DAYS_OF_WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
              "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}


@app.post("/settings/display")
def settings_save_display(
    request: Request,
    timezone: str = Form(...),
    language: str = Form("en"),
    theme: str = Form("system"),
):
    user, denied = _require_user(request)
    if denied:
        return denied

    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        timezone = "Europe/London"

    if language not in i18n.SUPPORTED_LOCALES:
        language = i18n.DEFAULT_LOCALE

    if theme not in ("light", "dark", "system"):
        theme = "system"

    config.set_value(user["id"], "timezone", timezone)
    config.set_value(user["id"], "language", language)
    config.set_value(user["id"], "theme", theme)
    return RedirectResponse(url="/settings?saved=display", status_code=303)


@app.post("/settings/credentials/anilist")
def settings_save_credentials_anilist(
    request: Request,
    anilist_username: str = Form(""),
    anilist_token: str = Form(""),
):
    """Issue #188 — one endpoint per credential card, so each card's own Save
    action only ever touches that card's fields. A single shared endpoint (the
    pre-#188 shape) would need every field resubmitted on every save, or a
    now-absent field would read as "user cleared this" and silently wipe out
    a different card's already-saved value."""
    user, denied = _require_user(request)
    if denied:
        return denied

    config.set_value(user["id"], "anilist_username", anilist_username.strip())
    if anilist_token.strip():
        config.set_value(user["id"], "anilist_token", anilist_token.strip())

    return RedirectResponse(url="/settings?saved=credentials_anilist", status_code=303)


@app.post("/settings/credentials/crunchyroll")
def settings_save_credentials_crunchyroll(
    request: Request,
    cr_etp_rt: str = Form(""),
):
    user, denied = _require_user(request)
    if denied:
        return denied

    if cr_etp_rt.strip():
        config.set_value(user["id"], "cr_etp_rt", cr_etp_rt.strip())

    return RedirectResponse(url="/settings?saved=credentials_crunchyroll", status_code=303)


@app.post("/settings/credentials/netflix")
def settings_save_credentials_netflix(
    request: Request,
    netflix_cookie_header: str = Form(""),
    netflix_profile_guid: str = Form(""),
):
    user, denied = _require_user(request)
    if denied:
        return denied

    if netflix_cookie_header.strip():
        config.set_value(user["id"], "netflix_cookie_header", netflix_cookie_header.strip())
    if netflix_profile_guid.strip():
        config.set_value(user["id"], "netflix_profile_guid", netflix_profile_guid.strip())

    return RedirectResponse(url="/settings?saved=credentials_netflix", status_code=303)


@app.post("/api/credentials/test/{provider}")
async def test_credential(provider: str, request: Request):
    """Issue #188 — "Test connection" per credential card. Validates whatever
    value is currently in the form (falling back to the already-saved one for
    any field left blank, matching the Save endpoints' own "blank = keep
    existing" convention) against the live service, using the exact same
    login/auth code the real sync scripts run — see app/credential_check.py's
    module docstring for why. Read-only: never writes anything, whether the
    check passes or fails."""
    user, denied = _require_user_api(request)
    if denied:
        return denied

    if provider not in ("anilist", "crunchyroll", "netflix"):
        return JSONResponse({"ok": False, "message": "Unknown provider."}, status_code=404)

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    def _field(name: str) -> str:
        submitted = (payload.get(name) or "").strip()
        return submitted or config.get(user["id"], name)

    if provider == "anilist":
        ok, detail = credential_check.check_anilist(_field("anilist_username"), _field("anilist_token"))
    elif provider == "crunchyroll":
        ok, detail = credential_check.check_crunchyroll(_field("cr_etp_rt"))
    else:
        ok, detail = credential_check.check_netflix(_field("netflix_cookie_header"), _field("netflix_profile_guid"))

    return JSONResponse({"ok": ok, "detail": detail})


@app.post("/settings/cr-overrides")
def settings_add_cr_override(
    request: Request,
    series_title: str = Form(...),
    season_number: int = Form(...),
    anilist_id: int = Form(...),
):
    """Issue #159 — manual per-user Crunchyroll (series_title, season_number) ->
    AniList id override, for a title the season-suffix heuristic still gets wrong
    (or leaves unmatched). Personal-layer table (cr_title_overrides) — only this
    route and its delete counterpart below ever write to it; sync_crunchyroll.py
    only reads it. Upserts on (user_id, series_title, season_number) so re-adding
    the same title/season just corrects the anilist_id rather than erroring.

    series_title is lowercased/trimmed before storing so it matches CR's raw
    series_title.lower() exactly — the same normalization
    find_anilist_id()/title_index already use, and what
    sync_crunchyroll.load_title_overrides() looks up by."""
    user, denied = _require_user(request)
    if denied:
        return denied

    title = series_title.strip().lower()
    if not title or season_number < 1 or anilist_id < 1:
        return RedirectResponse(
            url="/settings?cr_override_error=Series+title%2C+season%2C+and+AniList+ID+are+all+required",
            status_code=303,
        )

    db.execute(
        """
        INSERT INTO cr_title_overrides (user_id, series_title, season_number, anilist_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id, series_title, season_number)
        DO UPDATE SET anilist_id = EXCLUDED.anilist_id, updated_at = now()
        """,
        (user["id"], title, season_number, anilist_id),
    )

    return RedirectResponse(url="/settings?saved=cr_override", status_code=303)


@app.post("/settings/cr-overrides/{override_id}/delete")
def settings_delete_cr_override(request: Request, override_id: int):
    user, denied = _require_user(request)
    if denied:
        return denied

    db.execute(
        "DELETE FROM cr_title_overrides WHERE id = %s AND user_id = %s",
        (override_id, user["id"]),
    )
    return RedirectResponse(url="/settings?saved=cr_override_deleted", status_code=303)


@app.post("/settings/streaming-services")
async def settings_update_streaming_services(request: Request):
    """Issue #182 — replaces this user's full "services I own" set in one submit
    (a checkbox group over STREAMING_SITES, not a one-at-a-time add/delete flow like
    cr-overrides above) — read via request.form().getlist() rather than a typed
    FastAPI Form(list[str]) parameter, since the field name repeats once per checked
    box and this sidesteps any FastAPI/Starlette version-specific behavior around
    that. Silently drops any submitted value not in STREAMING_SITES (a tampered
    request, or a site since removed from the allowlist) rather than erroring —
    same allowlist enforcement point as every other STREAMING_SITES filter in this
    file, just on write instead of read."""
    user, denied = _require_user(request)
    if denied:
        return denied

    form = await request.form()
    selected = sorted({s for s in form.getlist("service") if s in STREAMING_SITES})

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_streaming_services WHERE user_id = %s", (user["id"],))
            for service in selected:
                cur.execute(
                    "INSERT INTO user_streaming_services (user_id, service) VALUES (%s, %s)",
                    (user["id"], service),
                )
        conn.commit()

    return RedirectResponse(url="/settings?saved=streaming_services", status_code=303)


def _parse_csv_import_summary(stdout: str) -> dict | None:
    """import_netflix_csv.py prints one final "IMPORT_RESULT: {...}" line on success —
    same spirit as run_full_sync.py's steps JSONB, just simpler since this is a single
    one-shot action rather than a multi-step pipeline. Returns None if the line is
    somehow missing (still exit 0, but nothing to show) rather than raising, since the
    import itself already succeeded by the time this is called."""
    for line in reversed(stdout.splitlines()):
        if line.startswith("IMPORT_RESULT: "):
            try:
                return json.loads(line[len("IMPORT_RESULT: "):])
            except json.JSONDecodeError:
                return None
    return None


@app.post("/settings/netflix-csv-import")
def settings_netflix_csv_import(request: Request, netflix_csv: UploadFile = File(...)):
    """Issue #98 — upload Netflix's own "download all" viewing-activity CSV export as a
    bootstrap alternative to the live paginated walk, for accounts whose full history is
    too large to reliably complete within Force Full Resync's timeout budget. Runs
    scripts/import_netflix_csv.py as its own subprocess — same USER_ID/credentials-env
    contract every other scripts/sync_*.py step uses — rather than importing/running its
    matching logic in-process, so a bad row or an AniList hiccup can't take the app
    server down with it, same isolation the rest of the sync pipeline already has.

    Synchronous, not backgrounded like /api/sync — this is a one-shot user action the
    settings page can just show a result for directly, not an ongoing job needing a
    poll loop."""
    user, denied = _require_user(request)
    if denied:
        return denied

    anilist_token = config.get(user["id"], "anilist_token")
    anilist_username = config.get(user["id"], "anilist_username")
    if not anilist_token or not anilist_username:
        return RedirectResponse(
            url="/settings?csv_import_error=AniList+credentials+must+be+configured+first",
            status_code=303,
        )

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            # UploadFile is capped by Starlette's own spooled-temp-file handling —
            # .file is a regular file object here, safe to read synchronously.
            tmp.write(netflix_csv.file.read())
            tmp_path = tmp.name

        env = os.environ.copy()
        env["USER_ID"] = str(user["id"])
        env["ANILIST_TOKEN"] = anilist_token
        env["ANILIST_USERNAME"] = anilist_username

        log_row = db.execute_returning(
            "INSERT INTO sync_log (user_id, type, status, steps) VALUES (%s, 'netflix_csv_import', 'running', '[]') "
            "RETURNING id",
            (user["id"],),
        )

        try:
            result = subprocess.run(
                [sys.executable, _NETFLIX_CSV_IMPORT_SCRIPT, tmp_path],
                capture_output=True, text=True, timeout=_NETFLIX_CSV_IMPORT_TIMEOUT, env=env,
            )
        except subprocess.TimeoutExpired:
            db.execute(
                "UPDATE sync_log SET status = 'error', error_msg = %s WHERE id = %s",
                (f"Import timed out after {_NETFLIX_CSV_IMPORT_TIMEOUT}s", log_row["id"]),
            )
            return RedirectResponse(url="/settings?csv_import_error=Import+timed+out", status_code=303)

        if result.returncode != 0:
            db.execute(
                "UPDATE sync_log SET status = 'error', error_msg = %s WHERE id = %s",
                (result.stderr[-800:] or "Import failed — check container logs", log_row["id"]),
            )
            return RedirectResponse(url="/settings?csv_import_error=Import+failed", status_code=303)

        summary = _parse_csv_import_summary(result.stdout)
        db.execute(
            "UPDATE sync_log SET status = 'ok', entries_updated = %s WHERE id = %s",
            (summary.get("updated") if summary else None, log_row["id"]),
        )
        return RedirectResponse(url="/settings?saved=netflix_csv_import", status_code=303)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@app.post("/settings/notifications")
def settings_save_notifications(
    request: Request,
    telegram_enabled: str | None = Form(None),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    discord_enabled: str | None = Form(None),
    discord_webhook_url: str = Form(""),
    ntfy_enabled: str | None = Form(None),
    ntfy_server_url: str = Form(""),
    ntfy_topic: str = Form(""),
    ntfy_auth_token: str = Form(""),
):
    user, denied = _require_user(request)
    if denied:
        return denied

    config.set_value(user["id"], "telegram_enabled", "true" if telegram_enabled else "false")
    config.set_value(user["id"], "telegram_chat_id", telegram_chat_id.strip())

    # Only overwrite the bot token if a non-empty value was submitted (empty = leave unchanged)
    if telegram_bot_token.strip():
        config.set_value(user["id"], "telegram_bot_token", telegram_bot_token.strip())

    config.set_value(user["id"], "discord_enabled", "true" if discord_enabled else "false")
    # Must look like a real Discord webhook URL — the destination host is otherwise
    # fully user-supplied, which would let a bad value point server-side POSTs at an
    # arbitrary internal address. An invalid value is silently dropped rather than
    # saved, matching how an invalid timezone/schedule value is handled elsewhere on
    # this page.
    if discord_webhook_url.strip() and DISCORD_WEBHOOK_RE.match(discord_webhook_url.strip()):
        config.set_value(user["id"], "discord_webhook_url", discord_webhook_url.strip())

    config.set_value(user["id"], "ntfy_enabled", "true" if ntfy_enabled else "false")
    config.set_value(user["id"], "ntfy_topic", ntfy_topic.strip())
    # ntfy_server_url, unlike Discord's webhook host, is meant to be user-configurable
    # (self-hosting is a supported use case) — only a scheme check plus a link-local
    # block apply here (see ntfy_host_blocked's docstring for why that's the one range
    # actually worth blocking, unlike RFC1918/loopback which are legitimate targets).
    stripped_ntfy_url = ntfy_server_url.strip()
    if not stripped_ntfy_url or (
        stripped_ntfy_url.startswith(("http://", "https://")) and not ntfy_host_blocked(stripped_ntfy_url)
    ):
        config.set_value(user["id"], "ntfy_server_url", stripped_ntfy_url)
    if ntfy_auth_token.strip():
        config.set_value(user["id"], "ntfy_auth_token", ntfy_auth_token.strip())

    return RedirectResponse(url="/settings?saved=notifications", status_code=303)


@app.post("/settings/schedule")
def settings_save_schedule(
    request: Request,
    sync_daily_time: str = Form("04:30"),
    sync_recommender_day: str = Form("sun"),
    sync_recommender_time: str = Form("05:00"),
):
    # Sync schedule is instance-wide (one cron trigger regardless of user count), so it
    # goes to instance_config rather than a per-user settings row — admin-only, since a
    # non-admin changing it would affect every other user's sync timing too.
    denied = _require_admin(request)
    if denied:
        return denied

    try:
        h, m = sync_daily_time.split(":")
        assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except Exception:
        sync_daily_time = "04:30"
    _instance_config_set("sync_daily_time", sync_daily_time)

    if sync_recommender_day not in DAYS_OF_WEEK:
        sync_recommender_day = "sun"
    _instance_config_set("sync_recommender_day", sync_recommender_day)

    try:
        h, m = sync_recommender_time.split(":")
        assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except Exception:
        sync_recommender_time = "05:00"
    _instance_config_set("sync_recommender_time", sync_recommender_time)

    # Apply new schedule immediately — no restart needed
    _apply_schedule()

    # Issue #96 — this form now lives on Admin → Instance Config, not Settings.
    return RedirectResponse(url="/admin?tab=instance-config&saved=schedule", status_code=303)


@app.post("/settings/privacy")
def settings_save_privacy(
    request: Request,
    hidden_tags: str = Form(""),
    anonymize_activity: str | None = Form(None),
):
    user, denied = _require_user(request)
    if denied:
        return denied

    # Privacy — see app/privacy.py. Never shown to other users until #22/#27 exist
    # and actually call into that module, but the controls themselves need to be
    # available before either of those ship, not added after the fact.
    tags = [t.strip() for t in hidden_tags.split(",") if t.strip()]
    config.set_value(user["id"], "hidden_tags", json.dumps(tags))
    config.set_value(user["id"], "anonymize_activity", "true" if anonymize_activity else "false")

    return RedirectResponse(url="/settings?saved=privacy", status_code=303)


@app.post("/settings/password")
def settings_set_password(request: Request, password: str = Form(...)):
    """Set or change the current session's local password, regardless of
    auth_provider — the symmetric counterpart to account linking, for accounts
    that started OAuth-only and previously had no way to get a password except
    an admin-mediated reset."""
    user, denied = _require_user(request)
    if denied:
        return denied

    if len(password) < 8:
        return RedirectResponse(
            url="/settings?password_error=Password+must+be+at+least+8+characters",
            status_code=303,
        )

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    db.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user["id"]))
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/settings/2fa/setup", response_class=HTMLResponse)
def settings_2fa_setup_page(request: Request, error: str = ""):
    """Issue #83 — start (or resume) a pending TOTP setup. The secret is generated
    once per setup attempt and held server-side, in-process, keyed by user_id (see
    _totp_setup_state's module-level comment above) — reloading this page mid-setup
    reuses the same pending secret (so the QR code the user already scanned stays
    valid) rather than silently generating a new one underneath them. If the
    previous pending secret has expired (_TOTP_SETUP_TTL_MINUTES), a fresh one is
    issued here too — the same TTL check POST already did — so GET and POST can
    never disagree about whether a given secret/QR is still current."""
    user, denied = _require_user(request)
    if denied:
        return denied
    if user["totp_enabled"]:
        return RedirectResponse(url="/settings", status_code=303)
    if not user["password_hash"]:
        return RedirectResponse(
            url="/settings?twofa_error=Set+a+password+first+-+two-factor+authentication+requires+a+local+password",
            status_code=303,
        )

    state = _totp_setup_state_for(user["id"])
    secret = state["secret"] if state else _start_totp_setup_state(user["id"])

    return templates.TemplateResponse(
        request,
        "settings_2fa_setup.html",
        {
            "qr_data_uri": _totp_qr_data_uri(secret, user["email"]),
            "secret": secret,
            "error": error,
        },
    )


@app.post("/settings/2fa/setup")
def settings_2fa_setup_confirm(request: Request, code: str = Form(...)):
    """Confirms setup by verifying a real code from the authenticator app before ever
    writing totp_secret/totp_enabled — this is the acceptance-criteria requirement
    that enabling 2FA can't just blind-save a secret nobody's proven they can produce
    valid codes from. On success, also generates and stores this account's one-time
    recovery codes (issue #83's lost-authenticator recovery mechanism) in the same
    transaction as enabling, so 2FA is never left "on" without a working recovery
    path."""
    user, denied = _require_user(request)
    if denied:
        return denied
    if user["totp_enabled"]:
        return RedirectResponse(url="/settings", status_code=303)

    state = _totp_setup_state_for(user["id"])
    if not state:
        return RedirectResponse(
            url="/settings/2fa/setup?error=Setup+session+expired+-+start+again", status_code=303
        )

    state["attempts"] += 1
    if state["attempts"] > _TOTP_SETUP_MAX_ATTEMPTS:
        _clear_totp_setup_state(user["id"])
        return RedirectResponse(
            url="/settings/2fa/setup?error=Too+many+attempts+-+start+again", status_code=303
        )

    secret = state["secret"]
    if not pyotp.TOTP(secret).verify(code.strip(), valid_window=1):
        return RedirectResponse(url="/settings/2fa/setup?error=Incorrect+code+-+try+again", status_code=303)

    recovery_codes = _generate_totp_recovery_codes()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET totp_secret = %s, totp_enabled = true, totp_enabled_at = now() WHERE id = %s",
                (config.encrypt_secret(secret), user["id"]),
            )
            for rc in recovery_codes:
                cur.execute(
                    "INSERT INTO totp_recovery_codes (user_id, code_hash) VALUES (%s, %s)",
                    (user["id"], _hash_recovery_code(rc)),
                )
        conn.commit()

    _clear_totp_setup_state(user["id"])
    # Shown exactly once, on the very next request only — settings_2fa_recovery_codes_page
    # pops this in-process entry on render, so a refresh/back-navigation can't re-display
    # codes that have already been shown. Never written to the database in plaintext, and
    # (like the setup secret above) never round-tripped through the client-visible
    # session cookie.
    _totp_recovery_display[user["id"]] = recovery_codes
    return RedirectResponse(url="/settings/2fa/recovery-codes", status_code=303)


@app.get("/settings/2fa/recovery-codes", response_class=HTMLResponse)
def settings_2fa_recovery_codes_page(request: Request):
    user, denied = _require_user(request)
    if denied:
        return denied
    codes = _totp_recovery_display.pop(user["id"], None)
    if not codes:
        return RedirectResponse(url="/settings", status_code=303)
    return templates.TemplateResponse(request, "settings_2fa_recovery_codes.html", {"codes": codes})


@app.post("/settings/2fa/disable")
def settings_2fa_disable(request: Request, password: str = Form(...)):
    """Disabling requires re-entering the current account password first (issue #83's
    explicit security requirement) — a hijacked session cookie alone can't silently
    turn 2FA off, since it doesn't carry the password. Removes the secret and every
    recovery code together so a later re-enable can't accidentally inherit stale
    codes from a previous setup.

    Security-review finding (post-#83): the password check here originally had no
    rate limiting at all, unlike every other password check in this app — someone
    holding a stolen session cookie but not the password got unlimited guesses to
    permanently strip 2FA off the account, directly defeating this route's own
    purpose. Now shares failed_login_attempts/locked_until with the login password
    check (this IS a password check, so it draws from the same guess budget as any
    other one) — not the separate totp_failed_attempts/totp_locked_until pair, which
    is specifically the TOTP-*code* guess budget used on the login second-factor
    step."""
    user, denied = _require_user(request)
    if denied:
        return denied
    if not user["totp_enabled"]:
        return RedirectResponse(url="/settings", status_code=303)

    if user["locked_until"] and user["locked_until"] > datetime.now(timezone.utc):
        minutes_left = max(1, int((user["locked_until"] - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
        return RedirectResponse(
            url=f"/settings?twofa_error=Too+many+failed+attempts.+Try+again+in+{minutes_left}+minutes",
            status_code=303,
        )

    valid = user["password_hash"] and bcrypt.checkpw(
        password.encode("utf-8"), user["password_hash"].encode("utf-8")
    )
    if not valid:
        attempts = user["failed_login_attempts"] + 1
        if attempts >= _LOGIN_MAX_ATTEMPTS:
            db.execute(
                "UPDATE users SET failed_login_attempts = %s, locked_until = now() + (%s * interval '1 minute') WHERE id = %s",
                (attempts, _LOGIN_LOCKOUT_MINUTES, user["id"]),
            )
        else:
            db.execute(
                "UPDATE users SET failed_login_attempts = %s WHERE id = %s",
                (attempts, user["id"]),
            )
        return RedirectResponse(url="/settings?twofa_error=Incorrect+password", status_code=303)

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM totp_recovery_codes WHERE user_id = %s", (user["id"],))
            cur.execute(
                "UPDATE users SET totp_secret = NULL, totp_enabled = false, totp_enabled_at = NULL, "
                "failed_login_attempts = 0, locked_until = NULL WHERE id = %s",
                (user["id"],),
            )
        conn.commit()

    return RedirectResponse(url="/settings?saved=twofa_disabled", status_code=303)


# ── Personal access tokens for the MCP server (issue #207) ──────────────────────
# Issue/revoke UI lives in Settings' "API Access" tab. Token generation/hashing/
# lookup itself is in app/pat.py, not here — main.py only owns the route/one-time-
# display plumbing, same split as sessions.py for the "active sessions" feature.


@app.post("/settings/tokens")
def settings_create_token(request: Request, name: str = Form(...), scope: str = Form(pat.SCOPE_READ)):
    """Issues a new PAT for the current user. The raw token is shown exactly once,
    on the very next request only (settings_token_created pops it from the
    in-process display dict on render) — same one-time-display pattern as issue
    #83's TOTP recovery codes (_totp_recovery_display above). Never written to the
    database in plaintext, never round-tripped through the client-visible session
    cookie.

    scope (issue #208): defaults to read-only unless the form explicitly submits
    read_write — pat.create_token itself also falls back to read-only for any
    value outside VALID_SCOPES, so a tampered/unexpected form value can't mint a
    write-capable token either."""
    user, denied = _require_user(request)
    if denied:
        return denied

    label = name.strip() or "Unnamed token"
    scope_val = scope if scope in pat.VALID_SCOPES else pat.SCOPE_READ
    _token_id, raw_token = pat.create_token(user["id"], label, scope=scope_val)
    _pat_display[user["id"]] = {"name": label, "token": raw_token, "scope": scope_val}
    return RedirectResponse(url="/settings/tokens/created", status_code=303)


@app.get("/settings/tokens/created", response_class=HTMLResponse)
def settings_token_created(request: Request):
    user, denied = _require_user(request)
    if denied:
        return denied
    display = _pat_display.pop(user["id"], None)
    if not display:
        return RedirectResponse(url="/settings", status_code=303)
    return templates.TemplateResponse(request, "settings_token_created.html", display)


@app.post("/settings/tokens/{token_id}/revoke")
def settings_revoke_token(request: Request, token_id: int):
    """Revoke one of the current user's own tokens (issue #207). pat.revoke_token
    scopes the UPDATE to (id, user_id), so passing another user's token_id here
    just finds no matching row and no-ops — same defense-in-depth shape as
    settings_revoke_session above."""
    user, denied = _require_user(request)
    if denied:
        return denied
    pat.revoke_token(user["id"], token_id)
    return RedirectResponse(url="/settings?saved=token_revoked", status_code=303)


@app.post("/api/sync")
async def trigger_sync(request: Request, background_tasks: BackgroundTasks):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    state = _get_sync_state(user["id"])
    with _sync_lock:
        if state["running"]:
            return JSONResponse({"status": "already_running"})
        state["running"] = True
        state["last_result"] = None
    background_tasks.add_task(_run_sync_task, user["id"], _FULL_SYNC_SCRIPT)
    return JSONResponse({"status": "started"})


@app.post("/api/sync/full-resync")
async def trigger_full_resync(request: Request, background_tasks: BackgroundTasks):
    """Issue #20 (Crunchyroll) + #21 (Netflix) — forced full re-walk of both providers'
    watch history for one run, ignoring their stored watermarks. Same single-flight
    guard as POST /api/sync since both ultimately touch the same per-user
    cr_sync_state / netflix_sync_state rows."""
    user, denied = _require_user_api(request)
    if denied:
        return denied

    state = _get_sync_state(user["id"])
    with _sync_lock:
        if state["running"]:
            return JSONResponse({"status": "already_running"})
        state["running"] = True
        state["last_result"] = None
    background_tasks.add_task(_run_sync_task, user["id"], _FULL_SYNC_SCRIPT, True)
    return JSONResponse({"status": "started"})


@app.get("/api/sync/log")
def sync_log(request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    rows = db.fetchall(
        "SELECT run_at, type, status, entries_updated, error_msg, steps, trigger "
        "FROM sync_log WHERE user_id = %s AND run_at >= now() - interval '7 days' "
        "ORDER BY run_at DESC",
        (user["id"],),
    )
    return JSONResponse([
        {
            "run_at": r["run_at"].isoformat(),
            "type": r["type"],
            "status": r["status"],
            "entries_updated": r["entries_updated"],
            "error_msg": r["error_msg"],
            "steps": r["steps"] or [],
            "trigger": r["trigger"],
        }
        for r in rows
    ])


@app.get("/api/sync/status")
def sync_status(request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    state = _get_sync_state(user["id"])
    row = db.fetchone(
        "SELECT status, steps FROM sync_log WHERE user_id = %s AND type IN ('full_sync', 'force_full_resync') "
        "ORDER BY run_at DESC LIMIT 1",
        (user["id"],),
    )
    # OR'd with the in-memory flag to cover the brief startup race between POST
    # /api/sync setting state["running"]=True and run_full_sync.py's subprocess
    # actually managing to INSERT its 'running' row — without this, a poll landing in
    # that window would read the previous, already-settled row.
    running = bool(state["running"] or (row and row["status"] == "running"))
    last_result = row["status"] if row and row["status"] != "running" else None
    steps = (row["steps"] if row else None) or []

    ts_row = db.fetchone(
        "SELECT MAX(synced_at) AS ts FROM library_entries WHERE user_id = %s", (user["id"],)
    )
    last_synced = ts_row["ts"].isoformat() if ts_row and ts_row["ts"] else None
    return JSONResponse({
        "running": running,
        "last_result": last_result,
        "steps": steps,
        "last_synced": last_synced,
    })


@app.get("/api/outbox/status")
def outbox_status(request: Request):
    """Issue #100 — aggregate status_sync_outbox counts for this user, across every
    source (UI bulk edits and provider sync alike, since #100 merged them into one
    outbox). Lets Settings show "staged, not yet delivered to AniList" instead of a
    flat ok/error now that AniList delivery is decoupled from a sync run finishing."""
    user, denied = _require_user_api(request)
    if denied:
        return denied

    rows = db.fetchall(
        "SELECT state, COUNT(*) AS cnt FROM status_sync_outbox WHERE user_id = %s GROUP BY state",
        (user["id"],),
    )
    by_state = {"pending": 0, "in_progress": 0, "failed": 0}
    for r in rows:
        by_state[r["state"]] = r["cnt"]
    return JSONResponse({"by_state": by_state})


# ── Drop-pattern mining (issue #73) ─────────────────────────────────────────
# Read-only aggregation of personal_notes.drop_reason / personal_tags against
# dropped library entries' genres/format/episodes. Never writes to
# personal_notes; the drop_reason/personal_tags capture UI is untouched.
#
# Minimum-sample gate: the panel only renders once a user has at least this many
# DROPPED entries with a non-empty drop_reason. Below that, any genre/word
# breakdown is just noise from one or two data points, not a real pattern — and
# an empty/near-empty panel would clutter the stats page for new users who
# haven't dropped much yet. 5 was picked (issue suggested 3-5) because the
# per-genre breakdown further splits an already-small sample; 5 distinct
# reasoned drops gives at least a couple of genres/words a chance to repeat.
DROP_PATTERN_MIN_SAMPLES = 5

# Deliberately simple stopword list for v1 keyword mining — no NLP/stemming,
# per the issue's explicit scope. Covers common function words plus a few
# domain words ("anime", "episode", "show"...) that would otherwise dominate
# every result without saying anything genre/reason-specific.
_DROP_REASON_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "it", "its", "it's", "to", "of", "in",
    "on", "for", "was", "were", "is", "are", "be", "been", "being", "i", "im",
    "i'm", "this", "that", "these", "those", "just", "so", "too", "very",
    "really", "with", "without", "not", "didn't", "couldn't", "wasn't",
    "doesn't", "don't", "after", "before", "because", "got", "get", "getting",
    "like", "felt", "feel", "feels", "at", "by", "as", "my", "me", "them",
    "then", "than", "from", "have", "had", "has", "having", "up", "out",
    "into", "about", "when", "which", "what", "who", "if", "no", "yes", "you",
    "your", "it'll", "will", "would", "could", "should", "one", "all", "more",
    "even", "still", "here", "there", "did", "do", "does", "again", "also",
    "dropped", "drop", "dropping", "show", "shows", "series", "anime",
    "episode", "episodes", "ep", "eps", "watching", "watch", "watched",
}


def _tokenize_drop_reason(text: str) -> list[str]:
    """Lowercase word extraction + stopword filter. No NLP/stemming — v1 per
    issue #73's explicit scope. Words <= 2 chars are dropped as noise."""
    words = re.findall(r"[a-zA-Z']+", text.lower())
    return [w for w in words if len(w) > 2 and w not in _DROP_REASON_STOPWORDS]


def _compute_drop_patterns(user_id: int) -> dict | None:
    rows = db.fetchall(
        """
        SELECT le.progress, a.genres, a.format, a.episodes, pn.drop_reason, pn.personal_tags
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        JOIN personal_notes pn ON pn.anime_id = a.id AND pn.user_id = le.user_id
        WHERE le.user_id = %s AND le.status = 'DROPPED'
          AND pn.drop_reason IS NOT NULL AND btrim(pn.drop_reason) <> ''
        """,
        (user_id,),
    )
    if len(rows) < DROP_PATTERN_MIN_SAMPLES:
        return None

    # Completed-per-genre counts, to turn raw drop counts into a rate
    # ("what % of your Isekai ever get dropped") rather than just a count that's
    # mostly a proxy for how much of that genre you watch in the first place.
    completed_genre_rows = db.fetchall(
        """
        SELECT genre, COUNT(*) AS cnt
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id,
             jsonb_array_elements_text(a.genres) AS genre
        WHERE le.status = 'COMPLETED' AND le.user_id = %s
        GROUP BY genre
        """,
        (user_id,),
    )
    completed_by_genre = {r["genre"]: int(r["cnt"]) for r in completed_genre_rows}

    genre_drop_counts: Counter = Counter()
    format_drop_counts: Counter = Counter()
    tag_counts: Counter = Counter()
    word_counts: Counter = Counter()
    progress_ratios = []

    for row in rows:
        for genre in (row["genres"] or []):
            genre_drop_counts[genre] += 1
        if row["format"]:
            format_drop_counts[row["format"]] += 1
        for tag in (row["personal_tags"] or []):
            if tag and tag.strip():
                tag_counts[tag.strip().lower()] += 1
        for word in _tokenize_drop_reason(row["drop_reason"]):
            word_counts[word] += 1
        if row["episodes"] and row["progress"] is not None and row["episodes"] > 0:
            progress_ratios.append(min(row["progress"] / row["episodes"], 1.0))

    # Genre drop rate, restricted to genres with a combined (completed + dropped)
    # sample of at least 3 — otherwise one dropped one-off in a genre you've
    # otherwise never touched shows up as a false "100% drop rate".
    genre_drop_rate = []
    for genre, dropped_cnt in genre_drop_counts.items():
        total = completed_by_genre.get(genre, 0) + dropped_cnt
        if total < 3:
            continue
        genre_drop_rate.append({
            "genre": genre,
            "rate": round(dropped_cnt / total * 100),
            "dropped": dropped_cnt,
            "total": total,
        })
    genre_drop_rate.sort(key=lambda r: (-r["rate"], -r["total"]))

    avg_progress_pct = (
        round(sum(progress_ratios) / len(progress_ratios) * 100) if progress_ratios else None
    )

    return {
        "sample_size": len(rows),
        "genre_drop_rate": genre_drop_rate[:8],
        "top_words": [{"word": w, "count": c} for w, c in word_counts.most_common(10)],
        "top_tags": [{"tag": t, "count": c} for t, c in tag_counts.most_common(10)],
        "top_formats": [{"format": f, "count": c} for f, c in format_drop_counts.most_common(6)],
        "avg_progress_pct": avg_progress_pct,
    }


def _yearly_completion_rows(user_id: int, year: int) -> list[dict]:
    """Every library_entries row whose finish_date falls in the given calendar year
    (Jan 1 - Dec 31), joined with the anime row. This is the single source query
    every year-comparison aggregation below is built from — issue #193 (extending
    #wrapup-card, issue #78) and the "living page" issue it's held for (#196) are
    both expected to call this directly rather than re-deriving their own row set,
    so the two surfaces can't silently disagree on what counts as "in year Y".

    Time-boundary choice: `finish_date`, per #163/#193's own decision — it's a live,
    AniList-synced field (confirmed by #77/#176), not a one-off backfill. It's also
    the *only* per-entry timestamp this schema has finer than "whenever the row was
    last synced" — there is no per-episode watched-at log (see schema.sql), so
    anything finer-grained than "which calendar year did this entry finish in" isn't
    reconstructable from existing data without a new sync job, which is out of scope
    per the issue.

    Deliberately independent of #wrapup-card's own `a.season_year`/`a.season` filter:
    that filter is release-year (when a show originally aired), a different concept
    from completion-year (when *you* finished it) that this function scopes by. Both
    happen to be driven by the same selected `year` value from the card's one
    dropdown per the issue's explicit instruction not to add a second selector.
    """
    return db.fetchall(
        """
        SELECT le.anime_id, le.finish_date, le.progress, le.score,
               a.title_english, a.title_romaji, a.genres, a.duration
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        WHERE le.user_id = %s
          AND le.finish_date IS NOT NULL
          AND le.finish_date >= %s AND le.finish_date <= %s
        """,
        (user_id, f"{year}-01-01", f"{year}-12-31"),
    )


def _biggest_binge_week(rows: list[dict]) -> dict | None:
    """Reusable aggregation #1 (issue #193): most episodes credited to any 7-day
    window within the year, using each entry's finish_date as a proxy for "when
    those episodes were watched" (see _yearly_completion_rows' docstring for why
    finish_date is the best available signal — there's no per-episode watched-at
    timestamp in this schema). Takes the row set _yearly_completion_rows returns —
    call that first, this is a pure function over its output.

    Standard sliding-window-max argument: the maximizing window's left edge can
    always be shifted right until it lands on an actual data point without
    decreasing the window's sum, so it's sufficient (and exact) to only test
    windows anchored at each distinct finish_date present, rather than every
    possible day in the year.
    """
    by_date: dict = {}
    for r in rows:
        d = r["finish_date"]
        by_date[d] = by_date.get(d, 0) + (r["progress"] or 0)
    if not by_date:
        return None
    dates = sorted(by_date)
    best_start, best_end, best_sum = None, None, -1
    for start in dates:
        end = start + timedelta(days=6)
        total = sum(cnt for d, cnt in by_date.items() if start <= d <= end)
        if total > best_sum:
            best_start, best_end, best_sum = start, end, total
    return {"start": best_start.isoformat(), "end": best_end.isoformat(), "episodes": best_sum}


def _status_distribution_snapshot(rows: list[dict]) -> dict:
    """Reusable aggregation #2 (issue #193): the "Planning -> Completed
    status-distribution snapshot" for the year, per #163's own phrasing of what
    that means — the count of entries that finished (reached a finish_date) in this
    calendar year. Takes _yearly_completion_rows' output.

    There's no status-transition history table (library_entries.status is
    current-state-only, see schema.sql), so the count of entries whose finish_date
    lands in this year is the closest reconstructable proxy for "how many moved
    from Planning to Completed that year" — every completion necessarily passed
    through a pre-completion state first. Returned as a dict (not a bare int) so
    this can grow a real multi-status breakdown later without changing callers'
    shape.
    """
    return {"planning_to_completed": len(rows)}


def _score_distribution(rows: list[dict]) -> list[dict]:
    """Low-level helper: {score: count} histogram (1-5) over any row set with a
    `score` field. Used by _score_distribution_shift below for both the selected
    year and the prior year, so the two sides are computed identically."""
    counts: Counter = Counter()
    for r in rows:
        if r["score"] is not None and r["score"] > 0:
            counts[int(r["score"])] += 1
    return [{"score": s, "count": c} for s, c in sorted(counts.items())]


def _score_distribution_shift(this_year_rows: list[dict], prior_year_rows: list[dict]) -> dict:
    """Reusable aggregation #3 (issue #193): year-over-year score-distribution
    shift. Takes two _yearly_completion_rows() results — the selected year's and
    year-1's — so the caller controls exactly which two years are compared.

    Graceful empty-prior-year handling (a user's first year on AniDex, or simply a
    year with no scored completions the year before): `has_prior_year_data` is
    False and `prior_year` is an empty list rather than an error, so callers render
    an empty state instead of a broken chart.
    """
    prior_year_scores = _score_distribution(prior_year_rows)
    return {
        "this_year": _score_distribution(this_year_rows),
        "prior_year": prior_year_scores,
        "has_prior_year_data": bool(prior_year_scores),
    }


def _year_comparison_extras(user_id: int, year: int) -> dict:
    """Orchestrates the three reusable aggregations above for #wrapup-card's
    year-comparison extension (issue #193). `year` is whatever calendar year the
    card's existing selector currently has picked; comparison is always against
    year-1. Kept as a thin wrapper — #196 (the living "year so far" page, held
    until this ships) can call this directly for the current year, or call the
    three aggregation functions individually if it needs different inputs.
    """
    rows = _yearly_completion_rows(user_id, year)
    prior_rows = _yearly_completion_rows(user_id, year - 1)
    return {
        "year": year,
        "prior_year": year - 1,
        "binge_week": _biggest_binge_week(rows),
        "status_snapshot": _status_distribution_snapshot(rows),
        "score_shift": _score_distribution_shift(rows, prior_rows),
    }


# ── "Your year so far" living page (issue #196, held on #193 above) ─────────────
#
# A dedicated, always-current-calendar-year destination — no year picker, unlike
# #wrapup-card's selector. Every one of the three metrics #193 built as reusable
# functions (_biggest_binge_week, _status_distribution_snapshot,
# _score_distribution_shift) is called directly here rather than through
# _year_comparison_extras, so this page doesn't pay for a second
# _yearly_completion_rows() query on top of the one it needs anyway for the
# top-genre/highest-rated/pace metrics that are new to this issue. That still
# means every shared metric goes through the exact same aggregation code #193
# shipped — nothing here reimplements binge-week/status-snapshot/score-shift logic.


def _pace_stat(
    this_year_rows: list[dict], prior_year_rows: list[dict], cutoff_month_day: tuple[int, int]
) -> dict:
    """Issue #164's pace stat, folded into #196: "on pace to match last year",
    framed as ahead/behind/on-pace rather than a numeric target (see #164's own
    gut-check against a nagging pace nudge — this is passive/visual only, no
    notification tied to it specifically).

    Compares this year's cumulative episodes as of `cutoff_month_day` against the
    prior year's cumulative total at that SAME (month, day) cutoff — not the prior
    year's full-year total, which would always read as "behind" until Dec 31.
    Pure function over two _yearly_completion_rows() row sets plus a cutoff
    (rather than reaching for date.today() itself), so it's directly
    unit-testable with known rows/cutoffs for two different years without
    needing a real "today" or a DB.

    Deliberately a (month, day) tuple rather than an ordinal day-of-year: two
    calendar dates that are "the same point in the year" don't share an ordinal
    day-of-year across a leap-year boundary (e.g. 2028-03-05 is day 65 of a leap
    year, but the equivalent 2027-03-05 is day 64) — comparing ordinals directly
    would let up to a day of the wrong year's data leak into the cumulative
    total. (month, day) tuples compare correctly regardless of either year's
    leap-ness.

    Anything within 5 percentage points either side of the prior year counts as
    "on_pace" rather than a coinflip ahead/behind on essentially a tie.
    """
    def _cumulative(rows: list[dict], cutoff: tuple[int, int]) -> int:
        episodes = 0
        for r in rows:
            fd = r["finish_date"]
            if (fd.month, fd.day) > cutoff:
                continue  # defensive: a finish_date beyond the cutoff shouldn't
                          # normally occur (both row sets are already scoped to
                          # their own calendar year), but never let one count
                          # towards a cumulative total it's not part of.
            episodes += r["progress"] or 0
        return episodes

    this_year_episodes = _cumulative(this_year_rows, cutoff_month_day)
    prior_year_episodes = _cumulative(prior_year_rows, cutoff_month_day)

    if prior_year_episodes == 0:
        status = "no_prior_data"
        diff_pct = None
    else:
        diff_pct = round((this_year_episodes - prior_year_episodes) / prior_year_episodes * 100)
        if abs(diff_pct) <= 5:
            status = "on_pace"
        elif diff_pct > 0:
            status = "ahead"
        else:
            status = "behind"

    return {
        "this_year_episodes": this_year_episodes,
        "prior_year_episodes": prior_year_episodes,
        "status": status,
        "diff_pct": diff_pct,
    }


def _compute_wrapped_page(user_id: int, today: date | None = None) -> dict:
    """Full data set for GET /stats/wrapped. `today` is injectable for tests
    (defaults to date.today()) — everything else is a pure function of it plus
    _yearly_completion_rows()' output for the current year and year-1.

    top_genre/highest_rated/total_episodes/total_minutes are new to this issue —
    computed directly off the same row set _yearly_completion_rows() already
    returns rather than a second query. binge_week/status_snapshot/score_shift
    are #193's three functions, called unmodified.
    """
    today = today or date.today()
    year = today.year

    rows = _yearly_completion_rows(user_id, year)
    prior_rows = _yearly_completion_rows(user_id, year - 1)

    total_episodes = sum(r["progress"] or 0 for r in rows)
    total_minutes = sum((r["progress"] or 0) * (r["duration"] or 24) for r in rows)
    total_hours = total_minutes // 60
    total_days = round(total_minutes / 1440, 1)
    # Single source for the >=24h "show days instead of hours" display convention,
    # rather than re-deriving the same threshold a third time in the template
    # (stats.html's JS already has it twice, for #wrapup-card and the headlines).
    total_watch_display = f"{total_days}d" if total_hours >= 24 else f"{total_hours}h"

    # Deterministic tie-breaks below: _yearly_completion_rows() has no ORDER BY,
    # so Postgres row order for a tied count/score is unspecified — pick*'s dict
    # iteration would otherwise let the displayed "top genre"/"highest rated"
    # flip between page loads for identical underlying data.
    genre_counts: Counter = Counter()
    for r in rows:
        for g in (r["genres"] or []):
            genre_counts[g] += 1
    top_genre = min(genre_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0] if genre_counts else None

    def _title(r: dict) -> str:
        return r["title_english"] or r["title_romaji"] or ""

    scored_rows = [r for r in rows if r["score"] is not None and r["score"] > 0]
    highest_rated = None
    if scored_rows:
        best = min(scored_rows, key=lambda r: (-r["score"], _title(r)))
        highest_rated = {"title": _title(best), "score": best["score"]}

    return {
        "year": year,
        "prior_year": year - 1,
        # total_episodes > 0, not just bool(rows): a COMPLETED row with a
        # finish_date but progress 0/NULL (a data-quality edge case — a sync
        # glitch, or a row synced before progress was backfilled) would
        # otherwise render a populated-looking page with all-zero headline
        # stats instead of the intended empty state.
        "has_data": total_episodes > 0,
        "total_episodes": total_episodes,
        "total_minutes": total_minutes,
        "total_hours": total_hours,
        "total_days": total_days,
        "total_watch_display": total_watch_display,
        "top_genre": top_genre,
        "highest_rated": highest_rated,
        "binge_week": _biggest_binge_week(rows),
        "status_snapshot": _status_distribution_snapshot(rows),
        "score_shift": _score_distribution_shift(rows, prior_rows),
        "pace": _pace_stat(rows, prior_rows, (today.month, today.day)),
    }


# ── Recommend -> outcome hit-rate + dismiss-reason distribution (issue #185) ────
#
# The recommender (scripts/run_recommender.py) has never had a feedback loop:
# recommendation_scores.dismissed/dismiss_reason is captured but never aggregated,
# and there's no positive signal linking "we recommended X" to "the user later
# added/rated X". This is pure instrumentation — it reads recommendation_scores
# and library_entries, computes a metric, and never writes to either table. It
# does not touch build_taste_profile(), candidate discovery, or scoring weights;
# see #146/#185 for the full audit and scope decision this is spun out of.
#
# "Hit" definition (documented here since it's the number every future
# recommender-quality change gets judged against — see #185's acceptance
# criteria):
#   A non-dismissed recommendation is a HIT if the same (user, anime) shows up in
#   library_entries with status WATCHING/PLANNING/COMPLETED, and the best
#   available evidence of *when* that happened — COALESCE(anilist_updated_at,
#   synced_at); AniList's own last-modified timestamp when we have it (most
#   accurate — set by sync_anilist.py's upsert whenever a pull-sync detects a
#   real change), falling back to our local synced_at (guaranteed non-null,
#   defaults on INSERT, and per issue #46's IS DISTINCT FROM guard only ever
#   advances on a genuine change, not a routine sync heartbeat) — falls within
#   HIT_WINDOW_DAYS of recommendation_scores.first_shown_at (issue #185/migration
#   017; NOT computed_at, which gets bumped to now() on every recommender rerun
#   and would make a long-lived recommendation look "just recommended" forever).
#   Optionally weighted by whether that entry was later rated >= 4 stars
#   (hits_rated_highly), per the issue's "optionally weighted by whether it was
#   later rated highly" suggestion.
#
#   Dismissed recommendations are excluded from both the numerator and the
#   denominator — the user explicitly said "not for me", so whatever they did
#   with the anime afterward (including a later independent add) isn't
#   attributable to the recommendation having worked, and must not inflate the
#   hit rate.
#
# HIT_WINDOW_DAYS = 30: the recommender's built-in scheduler reruns weekly, so a
# 30-day window covers roughly four rescoring cycles of visibility on the
# /recommendations page before an eventual add stops being reasonably
# attributable to having been recommended at all.
HIT_WINDOW_DAYS = 30


def _compute_recommendation_outcomes(user_id: int) -> dict | None:
    """Recommend->outcome hit-rate + dismiss-reason distribution for `user_id`.
    Returns None if this user has no recommendation_scores rows at all yet (never
    had a recommender run), so the stats-page card can stay hidden rather than
    show an empty/zero panel for a brand-new instance."""
    row = db.fetchone(
        """
        SELECT
            COUNT(*)                          AS total,
            COUNT(*) FILTER (WHERE NOT rs.dismissed) AS eligible,
            COUNT(*) FILTER (WHERE rs.dismissed)     AS dismissed_total,
            COUNT(*) FILTER (
                WHERE NOT rs.dismissed
                  AND le.status IN ('WATCHING', 'PLANNING', 'COMPLETED')
                  AND COALESCE(le.anilist_updated_at, le.synced_at) >= rs.first_shown_at
                  AND COALESCE(le.anilist_updated_at, le.synced_at)
                      <= rs.first_shown_at + make_interval(days => %s)
            ) AS hits,
            COUNT(*) FILTER (
                WHERE NOT rs.dismissed
                  AND le.status IN ('WATCHING', 'PLANNING', 'COMPLETED')
                  AND COALESCE(le.anilist_updated_at, le.synced_at) >= rs.first_shown_at
                  AND COALESCE(le.anilist_updated_at, le.synced_at)
                      <= rs.first_shown_at + make_interval(days => %s)
                  AND le.score >= 4
            ) AS hits_rated_highly
        FROM recommendation_scores rs
        LEFT JOIN library_entries le
               ON le.user_id = rs.user_id AND le.anime_id = rs.anime_id
        WHERE rs.user_id = %s
        """,
        (HIT_WINDOW_DAYS, HIT_WINDOW_DAYS, user_id),
    )
    total = int(row["total"])
    if total == 0:
        return None

    eligible = int(row["eligible"])
    hits = int(row["hits"])

    # NOTE: the output alias here deliberately isn't "reason" — recommendation_scores
    # already has a real `reason` column (the JSONB genre/tag/studio match reason from
    # scoring), which takes precedence over an output alias of the same name in a
    # GROUP BY clause. Naming it "reason" here silently grouped by that unrelated JSONB
    # column instead of dismiss_reason, collapsing every dismissed row into one bucket.
    dismiss_rows = db.fetchall(
        """
        SELECT COALESCE(NULLIF(btrim(dismiss_reason), ''), 'no_reason') AS dismiss_bucket,
               COUNT(*) AS cnt
        FROM recommendation_scores
        WHERE user_id = %s AND dismissed = true
        GROUP BY dismiss_bucket
        ORDER BY cnt DESC
        """,
        (user_id,),
    )

    return {
        "window_days": HIT_WINDOW_DAYS,
        "eligible": eligible,
        "hits": hits,
        "hit_rate": round(hits / eligible * 100) if eligible else None,
        "hits_rated_highly": int(row["hits_rated_highly"]),
        "dismissed_total": int(row["dismissed_total"]),
        "dismiss_reasons": [{"reason": r["dismiss_bucket"], "count": int(r["cnt"])} for r in dismiss_rows],
    }


REWATCH_MOST_REWATCHED_LIMIT = 10


def _compute_rewatch_stats(user_id: int) -> dict:
    """Rewatch data for /stats (issue #189) — built entirely from the existing
    library_entries.repeat_count column, already correctly populated from AniList's
    `repeat` field by sync_anilist.py. Purely read-only presentation, no new data
    collection and no schema changes.

    Scoped to COMPLETED/REPEATING entries: repeat_count defaults to 0 for every
    entry (including ones never actually watched, e.g. PLANNING), so counting the
    full library would pad the "0 rewatches" bucket with titles that were never
    finished even once. Restricting to titles that have actually been completed
    at least once keeps "0 rewatches" meaning "finished once, never rewatched"
    rather than "not watched at all". Unlike _compute_drop_patterns above, there's
    no minimum-sample gate here — no privacy/small-sample-noise concern with a
    user's own rewatch counts, so the section always renders (with an empty state
    if they have no completed titles yet, or no rewatches yet)."""
    dist_rows = db.fetchall(
        """
        SELECT
            CASE WHEN le.repeat_count >= 3 THEN '3+' ELSE le.repeat_count::text END AS bucket,
            COUNT(*) AS cnt
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        WHERE le.user_id = %s AND le.status IN ('COMPLETED', 'REPEATING')
        GROUP BY bucket
        """,
        (user_id,),
    )
    dist_by_bucket = {r["bucket"]: int(r["cnt"]) for r in dist_rows}
    distribution = [
        {"bucket": b, "count": dist_by_bucket.get(b, 0)} for b in ("0", "1", "2", "3+")
    ]

    most_rewatched_rows = db.fetchall(
        """
        SELECT COALESCE(a.title_english, a.title_romaji) AS title, le.repeat_count AS count
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        WHERE le.user_id = %s AND le.repeat_count > 0
          AND le.status IN ('COMPLETED', 'REPEATING')
        ORDER BY le.repeat_count DESC, title ASC
        LIMIT %s
        """,
        (user_id, REWATCH_MOST_REWATCHED_LIMIT),
    )

    return {
        "distribution": distribution,
        "most_rewatched": [{"title": r["title"], "count": int(r["count"])} for r in most_rewatched_rows],
        "total_completed_titles": sum(d["count"] for d in distribution),
        "total_rewatched_titles": sum(d["count"] for d in distribution if d["bucket"] != "0"),
    }


# Taste-profile drift (issue #176) -- how the genre mix of a user's COMPLETED
# library has shifted over time, derived entirely from library_entries.finish_date
# + anime.genres, per the approach validated (against synthetic data) in #77's
# comment thread -- no new schema, no snapshot table. The query below is the same
# bucket-by-time/join-genres/count shape #77 validated, ported to this file's
# existing `jsonb_array_elements_text(a.genres) AS genre` comma-join idiom (already
# used by genre_rows above and _compute_drop_patterns) instead of #77's own
# `CROSS JOIN LATERAL jsonb_array_elements(...)` -- same result set, just
# consistent with how every other genre-fanout query in this file is written.
#
# Bucket-granularity threshold: #77 validated quarterly buckets against a
# synthetic 180-title/4.5-year library (92% finish_date coverage, 17 non-empty
# quarterly buckets) but flagged -- without being able to validate it against
# real data -- that a library "under ~40-50 titles" would run quarterly buckets
# too sparse to say anything. TASTE_DRIFT_MIN_COMPLETED_FOR_QUARTERLY takes the
# top of that flagged range (50) as the concrete, stated cutoff this issue's
# acceptance criteria call for: at >=50 COMPLETED entries with a usable
# finish_date, bucket quarterly; below that, fall back to yearly buckets, which
# keeps roughly the same per-bucket density from a smaller total by using wider
# buckets. Deliberately count-based rather than span-based -- a short-span
# library just degrades to fewer non-empty buckets either way (not a readability
# problem the way an under-populated bucket is), so span doesn't need its own
# cutoff on top of this one.
TASTE_DRIFT_MIN_COMPLETED_FOR_QUARTERLY = 50
# Bounds the chart's series count regardless of how many distinct genres a
# library touches -- anything outside the top N overall collapses into "Other"
# rather than producing an unreadable stacked-bar legend.
TASTE_DRIFT_TOP_GENRES = 8


def _format_taste_drift_bucket_label(bucket_iso: str, granularity: str) -> str:
    year, month, _ = bucket_iso.split("-")
    if granularity == "year":
        return year
    quarter = (int(month) - 1) // 3 + 1
    return f"{year} Q{quarter}"


def _compute_taste_drift(user_id: int) -> dict | None:
    """Genre mix of this user's COMPLETED library, bucketed by finish_date, for
    the /stats "taste drift" section (issue #176). Returns None when the user has
    no COMPLETED entry with a usable finish_date yet (brand-new account, or an
    AniList library where completion dates were never set) -- the section stays
    hidden entirely in that case, same gating pattern as _compute_drop_patterns
    and _compute_recommendation_outcomes above, rather than rendering an empty
    chart.

    Entries with a NULL finish_date are excluded from bucketing (can't be placed
    on a time axis) but are still counted in total_completed vs. usable_completed
    so the caller can be transparent about coverage instead of silently
    presenting a partial picture as complete."""
    totals_row = db.fetchone(
        """
        SELECT
            COUNT(*) AS total_completed,
            COUNT(*) FILTER (WHERE finish_date IS NOT NULL) AS usable_completed
        FROM library_entries
        WHERE user_id = %s AND status = 'COMPLETED'
        """,
        (user_id,),
    )
    total_completed = int(totals_row["total_completed"])
    usable_completed = int(totals_row["usable_completed"])
    if usable_completed == 0:
        return None

    granularity = (
        "quarter" if usable_completed >= TASTE_DRIFT_MIN_COMPLETED_FOR_QUARTERLY else "year"
    )

    rows = db.fetchall(
        """
        SELECT date_trunc(%s, le.finish_date)::date AS bucket, genre, COUNT(*) AS cnt
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id,
             jsonb_array_elements_text(a.genres) AS genre
        WHERE le.user_id = %s AND le.status = 'COMPLETED' AND le.finish_date IS NOT NULL
        GROUP BY bucket, genre
        ORDER BY bucket, cnt DESC
        """,
        (granularity, user_id),
    )

    genre_totals: Counter = Counter()
    for row in rows:
        genre_totals[row["genre"]] += int(row["cnt"])
    top_genres = [g for g, _ in genre_totals.most_common(TASTE_DRIFT_TOP_GENRES)]
    top_genre_set = set(top_genres)
    has_other = any(row["genre"] not in top_genre_set for row in rows)
    series_genres = top_genres + (["Other"] if has_other else [])

    buckets_seen: list[str] = []
    matrix: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket_iso = row["bucket"].isoformat()
        if bucket_iso not in matrix:
            matrix[bucket_iso] = {}
            buckets_seen.append(bucket_iso)
        genre = row["genre"] if row["genre"] in top_genre_set else "Other"
        matrix[bucket_iso][genre] = matrix[bucket_iso].get(genre, 0) + int(row["cnt"])
    buckets_seen.sort()

    buckets_out = [
        {
            "bucket": b,
            "label": _format_taste_drift_bucket_label(b, granularity),
            "counts": {g: matrix[b].get(g, 0) for g in series_genres},
        }
        for b in buckets_seen
    ]

    return {
        "granularity": granularity,
        "genres": series_genres,
        "buckets": buckets_out,
        "total_completed": total_completed,
        "usable_completed": usable_completed,
    }


# Studio loyalty (issue #223) -- per-studio average score vs. volume watched, a
# scatter/ranked comparison derived entirely from anime.studios + library_entries.score,
# same query-only pattern as taste drift (#176) above -- no schema change, no new
# table. The issue's own text says "anime.studio" but the actual column (see
# schema.sql) is `studios`, a JSONB array of {"name": ..., "isMain": ...} -- a title
# can carry several credited studios (e.g. animation studio + a co-producer). Only
# isMain=true entries count here, matching the exact convention
# scripts/run_recommender.py's _build_taste_profile/_score_candidate already use for
# studio weighting/matching -- keeps "which studio a title counts toward" consistent
# across the whole app rather than inventing a second definition.
#
# "Volume watched" is scoped to COMPLETED entries only (matches taste drift's own
# COMPLETED-only scoping) -- a title still WATCHING/PLANNING hasn't actually
# contributed a rating or a finished watch yet, so counting it as "loyalty" volume
# would be premature.
#
# Minimum-title threshold, per the issue's explicit ask ("require at least 2-3
# completed titles from a studio before it's plotted"): STUDIO_LOYALTY_MIN_TITLES
# takes 3, the same ">=3 combined sample" floor _compute_drop_patterns' per-genre
# drop-rate already uses above for the identical "don't let a single one-off title
# stand in for a signal" reasoning -- reusing an already-established number in this
# file rather than picking a fresh one. Below this, a studio is excluded from the
# view entirely (excluded_low_volume) rather than shown with a "low confidence"
# asterisk -- 1-2 completed titles isn't a partial signal worth flagging, it's not a
# signal. Separately, a studio can clear the title-count threshold but still have
# zero *scored* completions (AniList entries synced with progress but no rating) --
# there's no average to plot at all in that case, so those are excluded too
# (excluded_unscored), tracked separately from low-volume so the sub-line stays
# accurate about *why* a studio isn't shown rather than lumping both reasons
# together.
STUDIO_LOYALTY_MIN_TITLES = 3


def _compute_studio_loyalty(user_id: int) -> dict | None:
    """Per-studio (main studio only) average score vs. completed-title count, for
    the /stats "studio loyalty" section (issue #223). Returns None when the user has
    no COMPLETED entry attributed to any main studio yet -- the section stays hidden
    entirely, same gating pattern as _compute_taste_drift/_compute_drop_patterns
    above, rather than rendering an empty chart."""
    rows = db.fetchall(
        """
        SELECT
            studio_elem->>'name' AS studio,
            COUNT(*) AS title_count,
            COUNT(*) FILTER (WHERE le.score IS NOT NULL AND le.score > 0) AS scored_count,
            AVG(le.score) FILTER (WHERE le.score IS NOT NULL AND le.score > 0) AS avg_score
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id,
             jsonb_array_elements(a.studios) AS studio_elem
        WHERE le.user_id = %s AND le.status = 'COMPLETED'
          AND (studio_elem->>'isMain')::boolean
        GROUP BY studio_elem->>'name'
        """,
        (user_id,),
    )
    if not rows:
        return None

    studios_out = []
    excluded_low_volume = 0
    excluded_unscored = 0
    for row in rows:
        title_count = int(row["title_count"])
        scored_count = int(row["scored_count"])
        if title_count < STUDIO_LOYALTY_MIN_TITLES:
            excluded_low_volume += 1
            continue
        if scored_count == 0:
            excluded_unscored += 1
            continue
        studios_out.append({
            "studio": row["studio"],
            "title_count": title_count,
            "scored_count": scored_count,
            "avg_score": round(float(row["avg_score"]), 2),
        })

    if not studios_out:
        return None

    studios_out.sort(key=lambda s: (-s["avg_score"], -s["title_count"]))

    return {
        "studios": studios_out,
        "min_titles": STUDIO_LOYALTY_MIN_TITLES,
        "total_studios": len(rows),
        "excluded_low_volume": excluded_low_volume,
        "excluded_unscored": excluded_unscored,
    }


# Anime movies genuinely have a meaningful per-title runtime (a.duration is the
# real film length from AniList) -- 24 minutes (the TV-episode fallback used
# elsewhere for anime.duration) would badly understate a ~90-100 minute movie
# with no recorded duration, so format-split watch time gets its own default.
MOVIE_DEFAULT_DURATION_MINUTES = 100


def _compute_format_watch_time(user_id: int) -> dict:
    """Anime-native watch-time split (issue #224): episode count for
    TV/OVA/ONA/SPECIAL-format entries vs. movie-runtime minutes for MOVIE-format
    entries, instead of blending both into one "minutes" figure via a single
    assumed per-episode duration (the existing totals.watch_hours/watch_minutes
    stat in this same endpoint does that blending and is left as-is for
    backward compatibility with existing consumers of /api/stats, notably
    app/mcp_server.py's stats tool and the /stats "Year in anime" card -- this
    is an additive, separately-rendered stat, not a replacement).

    Always renders, no minimum-sample gate -- same reasoning as
    _compute_rewatch_stats: a user's own watch counts carry no small-sample or
    privacy concern, it just falls back to a zero/empty display.
    """
    row = db.fetchone(
        """
        SELECT
            COALESCE(SUM(le.progress) FILTER (WHERE a.format IS DISTINCT FROM 'MOVIE'), 0) AS episode_count,
            COUNT(*) FILTER (WHERE a.format = 'MOVIE' AND le.progress > 0) AS movie_count,
            COALESCE(SUM(le.progress * COALESCE(a.duration, %s)) FILTER (WHERE a.format = 'MOVIE'), 0) AS movie_minutes
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        WHERE le.user_id = %s
        """,
        (MOVIE_DEFAULT_DURATION_MINUTES, user_id),
    )
    movie_minutes = int(row["movie_minutes"])
    return {
        "episode_count": int(row["episode_count"]),
        "movie_count": int(row["movie_count"]),
        "movie_minutes": movie_minutes,
        "movie_hours": round(movie_minutes / 60, 1),
    }


# Season -> approximate real-world start month of that AniList season, used only
# to anchor the "started while airing" window below (a.season/a.season_year are
# quarter-level, not a real air-start date field on `anime`).
SEASONAL_FOLLOW_THROUGH_SEASON_START_MONTH = {
    "WINTER": 1, "SPRING": 4, "SUMMER": 7, "FALL": 10,
}

# A show can start airing up to ~10 days before the "official" quarter boundary
# (e.g. a late-March premiere counted as a SPRING show); a viewer who joins the
# simulcast anywhere in the first two months of a ~12-13 week cour is still
# reasonably "following it seasonally" even if they weren't watching week 1.
# Past this grace window, treat it as a later binge rather than a seasonal watch
# -- per issue #224's explicit out-of-scope note: someone who Plans a show for
# months then binges it later must NOT be counted as having followed it.
SEASONAL_FOLLOW_THROUGH_PRE_BUFFER_DAYS = 10
SEASONAL_FOLLOW_THROUGH_GRACE_WEEKS = 8

# Matches DROP_PATTERN_MIN_SAMPLES's precedent/rationale above: below this a
# percentage is more noise than signal, so the section stays hidden rather than
# showing a misleadingly precise-looking rate off 1-2 data points.
SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES = 5


def _compute_seasonal_follow_through(user_id: int) -> dict | None:
    """Of the anime this user started watching during its original airing
    window, what fraction actually got kept up with (issue #224) -- the
    anime-native counterpart to a general completion-rate stat, since TV/general
    trackers have no equivalent "simulcast" concept.

    Why this isn't built from airing_schedule_cache (as the issue's Context
    section initially suggested): that table is a forward-looking cache only --
    scripts/sync_airing_schedule.py's AniList query is `notYetAired: true`, and
    each refresh DELETEs and re-inserts only still-upcoming episodes for
    currently-RELEASING anime. Once an episode airs (or the whole show
    finishes), its row is gone -- there is no historical per-episode air-date
    record anywhere in this schema for a show that has already finished
    airing, which is most of a typical library. `anime.season`/`season_year`
    are the only *persistent* signal of an anime's original airing window, so
    that's what this uses instead, anchored to a real calendar date via
    SEASONAL_FOLLOW_THROUGH_SEASON_START_MONTH.

    "Started while airing" = library_entries.start_date (AniList's own
    startedAt, real user watch-start data, not a sync artifact) falls within
    [season_start - PRE_BUFFER_DAYS, season_start + GRACE_WEEKS]. Movies are
    excluded (format = 'MOVIE') -- "following seasonally" is a weekly-release
    concept that doesn't apply to a single-sitting watch. PLANNING entries are
    excluded -- no real watch has started yet.

    Each qualifying entry is then classified:
      - followed through: status COMPLETED or REPEATING (finished it, possibly
        more than once)
      - dropped or stalled: status DROPPED, or status WATCHING/PAUSED where the
        anime itself has already finished airing (anime.status = 'FINISHED') --
        the show is over and they still haven't caught up, which is a "didn't
        keep up" outcome even without an explicit drop
      - excluded (no verdict yet): status WATCHING/PAUSED while the anime is
        still RELEASING -- can't say whether they kept up until the season
        actually finishes airing

    Returns None below SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES judged entries (not
    still-airing-excluded ones) -- same small-sample gating rationale as
    _compute_drop_patterns.
    """
    rows = db.fetchall(
        """
        SELECT le.status AS entry_status, le.start_date,
               a.status AS anime_status, a.season, a.season_year
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        WHERE le.user_id = %s
          AND le.status != 'PLANNING'
          AND le.start_date IS NOT NULL
          AND a.season IS NOT NULL
          AND a.season_year IS NOT NULL
          AND a.format IS DISTINCT FROM 'MOVIE'
        """,
        (user_id,),
    )

    followed_through = 0
    dropped_or_stalled = 0
    excluded_still_airing = 0
    for row in rows:
        start_month = SEASONAL_FOLLOW_THROUGH_SEASON_START_MONTH.get(row["season"])
        if start_month is None:
            continue
        season_start = date(row["season_year"], start_month, 1)
        window_start = season_start - timedelta(days=SEASONAL_FOLLOW_THROUGH_PRE_BUFFER_DAYS)
        window_end = season_start + timedelta(weeks=SEASONAL_FOLLOW_THROUGH_GRACE_WEEKS)
        if not (window_start <= row["start_date"] <= window_end):
            continue  # started well outside the airing window -- a later binge, not seasonal

        status = row["entry_status"]
        if status in ("COMPLETED", "REPEATING"):
            followed_through += 1
        elif status == "DROPPED":
            dropped_or_stalled += 1
        elif status in ("WATCHING", "PAUSED") and row["anime_status"] == "FINISHED":
            dropped_or_stalled += 1
        else:
            excluded_still_airing += 1

    judged = followed_through + dropped_or_stalled
    if judged < SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES:
        return None

    return {
        "rate": round(followed_through / judged * 100),
        "followed_through": followed_through,
        "dropped_or_stalled": dropped_or_stalled,
        "judged": judged,
        "excluded_still_airing": excluded_still_airing,
    }


def _compute_watch_activity_times(user_id: int) -> dict | None:
    """Hour-of-day / day-of-week watch-activity breakdown for /stats (issue #222),
    Trakt-style, alongside the existing date-based heatmap (issue #10). Purely a
    query over library_entries -- no schema change.

    Timestamp choice: anilist_updated_at is the only library_entries column with
    real time-of-day resolution -- start_date/finish_date are DATE-only, and the
    schema has no per-episode watch-event log. This measures "when AniList last
    recorded a change to this entry" (progress, status, or score), not a literal
    per-episode watch instant -- the closest available proxy for "when a user
    watched", and the same field _compute_recommendation_outcomes already leans on
    for similar "when did this happen" reasoning. Unlike that call site though,
    this function deliberately does NOT fall back to COALESCE(anilist_updated_at,
    synced_at): synced_at is when *our sync job* ran, not the user, and since sync
    runs on a fixed schedule, falling back to it here would artificially stack
    every such entry at the sync cron's hour/weekday and distort the very
    distribution this chart exists to show. Entries with no anilist_updated_at are
    simply excluded instead.

    Entries that are still PLANNING with zero progress are excluded too -- for
    those, anilist_updated_at only reflects "added to my list", not "watched
    something", so counting them would mix in list-curation activity as if it were
    viewing activity.

    Timezone: converted to this user's own configured timezone
    (config.get(user_id, "timezone"), set on Settings -> Preferences) before
    bucketing, using the exact same ZoneInfo-with-UTC-fallback pattern /upcoming
    already uses to localize airing_schedule_cache timestamps. Issue #222 says not
    to introduce a *new* per-user timezone setting for this -- it doesn't need to,
    since one already exists and /upcoming already establishes how to convert a
    raw TIMESTAMPTZ with it; that's what "whatever timezone handling the rest of
    /stats already relies on" means here, not literal UTC. Falls back to UTC on an
    invalid/unrecognized zone name, same as /upcoming. This means the conversion
    has to happen in Python against a ZoneInfo, not via a SQL EXTRACT() against
    the DB session's own timezone -- so day-of-week bucketing below follows
    Python's datetime.weekday() convention (Monday=0 .. Sunday=6), matching
    /upcoming's week_grid indexing, not Postgres's EXTRACT(DOW) convention
    (Sunday=0) that an all-SQL version would have used.

    Returns None when the user has no usable timestamp at all (brand-new account,
    or every entry excluded by the two filters above) -- the section stays hidden
    entirely rather than rendering an all-zero chart, same gating pattern as
    _compute_taste_drift above.
    """
    rows = db.fetchall(
        """
        SELECT anilist_updated_at
        FROM library_entries
        WHERE user_id = %s
          AND anilist_updated_at IS NOT NULL
          AND NOT (status = 'PLANNING' AND progress = 0)
        """,
        (user_id,),
    )
    if not rows:
        return None

    tz_name = config.get(user_id, "timezone")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = timezone.utc

    by_day = [0] * 7    # index 0 = Monday .. 6 = Sunday (datetime.weekday() convention)
    by_hour = [0] * 24  # index 0..23, local hour
    for row in rows:
        local = row["anilist_updated_at"].astimezone(tz)
        by_day[local.weekday()] += 1
        by_hour[local.hour] += 1

    return {
        "total": len(rows),
        "by_day": by_day,
        "by_hour": by_hour,
    }


@app.get("/api/stats")
def stats_data(request: Request, year: int | None = None, season: str | None = None):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    # "Year in anime" wrap-up filter (issue #78) — optional, additive scoping of every
    # aggregate below to a single release year (and optionally season), using the same
    # a.season_year / a.season columns the "Completed by year" breakdown already groups
    # by. Omitted entirely, behavior is identical to the unfiltered stats page.
    wrap_conditions = []
    wrap_params: list = []
    applied_season = None
    if year is not None:
        wrap_conditions.append("a.season_year = %s")
        wrap_params.append(year)
    if season is not None and season.upper() in {"WINTER", "SPRING", "SUMMER", "FALL"}:
        applied_season = season.upper()
        wrap_conditions.append("a.season = %s")
        wrap_params.append(applied_season)
    wrap_filter_sql = (" AND " + " AND ".join(wrap_conditions)) if wrap_conditions else ""

    status_rows = db.fetchall(
        "SELECT le.status, COUNT(*) AS cnt FROM library_entries le "
        "JOIN anime a ON a.id = le.anime_id WHERE le.user_id = %s" + wrap_filter_sql + " "
        "GROUP BY le.status ORDER BY cnt DESC",
        (user["id"], *wrap_params),
    )
    score_rows = db.fetchall(
        "SELECT le.score::int AS score, COUNT(*) AS cnt FROM library_entries le "
        "JOIN anime a ON a.id = le.anime_id "
        "WHERE le.score IS NOT NULL AND le.user_id = %s" + wrap_filter_sql + " "
        "GROUP BY le.score ORDER BY le.score",
        (user["id"], *wrap_params),
    )
    genre_rows = db.fetchall(
        """
        SELECT genre, COUNT(*) AS cnt
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id,
             jsonb_array_elements_text(a.genres) AS genre
        WHERE le.status = 'COMPLETED' AND le.user_id = %s""" + wrap_filter_sql + """
        GROUP BY genre ORDER BY cnt DESC LIMIT 12
        """,
        (user["id"], *wrap_params),
    )
    year_rows = db.fetchall(
        """
        SELECT a.season_year AS year, COUNT(*) AS cnt
        FROM library_entries le JOIN anime a ON a.id = le.anime_id
        WHERE le.status = 'COMPLETED' AND a.season_year IS NOT NULL AND a.season_year >= 2010
          AND le.user_id = %s
        GROUP BY a.season_year ORDER BY a.season_year
        """,
        (user["id"],),
    )
    totals = db.fetchone(
        """
        SELECT
            COUNT(*) FILTER (WHERE le.status = 'COMPLETED') AS completed,
            COUNT(*) FILTER (WHERE le.status = 'WATCHING')  AS watching,
            COUNT(*) FILTER (WHERE le.status = 'DROPPED')   AS dropped,
            COALESCE(SUM(le.progress), 0)                   AS total_episodes,
            COALESCE(SUM(le.progress * COALESCE(a.duration, 24)), 0) AS watch_minutes,
            ROUND(AVG(le.score) FILTER (WHERE le.score IS NOT NULL AND le.score > 0), 1) AS mean_score
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        WHERE le.user_id = %s""" + wrap_filter_sql + """
        """,
        (user["id"], *wrap_params),
    )
    # Watch-activity heatmap (issue #10) — coarse granularity by design: the only
    # per-day signal available without new instrumentation is `finish_date`
    # (one date per completed series). Activity for a given day is approximated as
    # the total progress (episodes watched) of series that finished that day, over
    # a rolling 365-day window, zero-filled so the frontend can render a full grid
    # without guessing which days are missing. See CLAUDE.md Out of scope note on
    # this issue — no new progress-history table.
    heatmap_rows = db.fetchall(
        """
        WITH date_range AS (
            SELECT generate_series(
                (CURRENT_DATE - INTERVAL '364 days')::date,
                CURRENT_DATE,
                '1 day'::interval
            )::date AS day
        ),
        activity AS (
            SELECT finish_date AS day, COALESCE(SUM(progress), 0) AS cnt
            FROM library_entries
            WHERE user_id = %s
              AND finish_date IS NOT NULL
              AND finish_date >= CURRENT_DATE - INTERVAL '364 days'
            GROUP BY finish_date
        )
        SELECT dr.day, COALESCE(a.cnt, 0) AS cnt
        FROM date_range dr
        LEFT JOIN activity a ON a.day = dr.day
        ORDER BY dr.day
        """,
        (user["id"],),
    )
    completed = int(totals["completed"])
    dropped = int(totals["dropped"])
    completion_rate = round(completed / (completed + dropped) * 100) if (completed + dropped) > 0 else None
    watch_minutes = int(totals["watch_minutes"])
    watch_hours = watch_minutes // 60
    watch_days = round(watch_minutes / 1440, 1)
    genres_out = [{"genre": r["genre"], "count": r["cnt"]} for r in genre_rows]
    drop_patterns = _compute_drop_patterns(user["id"])  # issue #73, None below the sample-size gate
    recommendation_outcomes = _compute_recommendation_outcomes(user["id"])  # issue #185, None if never recommended anything
    rewatch_stats = _compute_rewatch_stats(user["id"])  # issue #189
    taste_drift = _compute_taste_drift(user["id"])  # issue #176, None if no usable finish_date yet
    studio_loyalty = _compute_studio_loyalty(user["id"])  # issue #223, None below the per-studio title threshold
    format_split = _compute_format_watch_time(user["id"])  # issue #224
    seasonal_follow_through = _compute_seasonal_follow_through(user["id"])  # issue #224, None below the sample-size gate
    watch_activity = _compute_watch_activity_times(user["id"])  # issue #222, None if no usable timestamp yet

    # Year-comparison extension of #wrapup-card (issue #193) — binge week, status
    # snapshot, score-distribution shift. Reuses #wrapup-card's *existing* `year`
    # selector value rather than adding a second one; only computed when that
    # selector actually has a year picked (same gate `year is not None` already
    # uses above for the release-year filter). Deliberately completion-year
    # (finish_date) scoped, not release-year (a.season_year) scoped, per #163's
    # time-boundary decision — see _yearly_completion_rows' docstring for why the
    # two concepts differ despite sharing this one input value.
    wrap_extras = _year_comparison_extras(user["id"], year) if year is not None else None

    return JSONResponse({
        "status": [{"label": r["status"].title(), "value": r["cnt"]} for r in status_rows],
        "scores": [{"score": r["score"], "count": r["cnt"]} for r in score_rows],
        "genres": genres_out,
        "top_genre": genres_out[0]["genre"] if genres_out else None,
        "by_year": [{"year": r["year"], "count": r["cnt"]} for r in year_rows],
        "heatmap": [{"date": r["day"].isoformat(), "count": int(r["cnt"])} for r in heatmap_rows],
        "drop_patterns": drop_patterns,
        "recommendation_outcomes": recommendation_outcomes,
        "rewatch": rewatch_stats,
        "taste_drift": taste_drift,
        "studio_loyalty": studio_loyalty,
        "format_split": format_split,
        "seasonal_follow_through": seasonal_follow_through,
        "watch_activity": watch_activity,
        "totals": {
            "completed": completed,
            "watching": int(totals["watching"]),
            "total_episodes": int(totals["total_episodes"]),
            "watch_hours": watch_hours,
            "watch_days": watch_days,
            "completion_rate": completion_rate,
            "mean_score": float(totals["mean_score"]) if totals["mean_score"] else None,
        },
        "wrap_filter": {"year": year, "season": applied_season},
        "wrap_extras": wrap_extras,
    })


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    user, denied = _require_user(request)
    if denied:
        return denied

    # Issue #182 — small summary card only (top un-owned-service pick), not the
    # full ranked list — that stays on the dedicated /streaming page. Computed
    # server-side here rather than via its own fetch()/API endpoint like the rest
    # of this page's charts: it's a single top-of-list lookup, not worth a second
    # round trip, and unlike the chart data below it doesn't need client-side
    # filtering (year/season pickers etc).
    coverage = _compute_streaming_coverage(user["id"])
    streaming_top = coverage["ranked"][0] if coverage["ranked"] else None

    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "streaming_top": streaming_top,
            "streaming_owned_count": len(coverage["owned"]),
        },
    )


@app.get("/stats/wrapped", response_class=HTMLResponse)
def stats_wrapped(request: Request):
    """Issue #196 — "Your year so far": the living, always-current-calendar-year
    counterpart to #wrapup-card's year-picker on /stats. Fully server-rendered
    (unlike /stats' charts, which fetch /api/stats) since there's no client-side
    filtering here — every visit just reflects wherever the current year is today.
    """
    user, denied = _require_user(request)
    if denied:
        return denied

    wrapped = _compute_wrapped_page(user["id"])

    return templates.TemplateResponse(request, "wrapped.html", {"wrapped": wrapped})


# Security-review finding (issue #314): /api/export and /admin/export-all return a
# user's (or, for admin, *every* user's) full personal library data in one response,
# and previously had zero in-app throttle — an authenticated session (the account's
# own, or a stolen/scripted one) could hammer either endpoint with no limit. Cloudflare
# Access sits in front of this instance (per CLAUDE.local.md) but that's a login/identity
# gate, not a request-rate control, so it doesn't cover repeated calls from an already-
# authenticated session. In-memory fixed-window counter, same single-process assumption
# as _totp_setup_state above (no --workers flag, see Dockerfile CMD) — a process restart
# just resets everyone's window, not a security gap. Keyed by user id (not IP), so it
# tracks the actual account regardless of proxy/IP churn in front of the app. A lock
# guards the shared dict since FastAPI runs sync `def` routes like these in a thread
# pool — concurrent requests from the same account are exactly the burst this exists
# to catch, so the check itself must be safe under real concurrency.
_export_rate_limit_state: dict[str, list[float]] = {}  # key -> recent request timestamps
_export_rate_limit_lock = threading.Lock()


def _check_export_rate_limit(key: str, max_requests: int, window_seconds: float) -> tuple[bool, int]:
    """Trailing-window rate check. Returns (allowed, retry_after_seconds). Records the
    request immediately when allowed, so the count reflects requests actually let
    through rather than merely attempted."""
    now = time.monotonic()
    cutoff = now - window_seconds
    with _export_rate_limit_lock:
        timestamps = _export_rate_limit_state.setdefault(key, [])
        while timestamps and timestamps[0] < cutoff:
            timestamps.pop(0)
        if len(timestamps) >= max_requests:
            retry_after = max(1, int(window_seconds - (now - timestamps[0])) + 1)
            return False, retry_after
        timestamps.append(now)
        return True, 0


def _export_rate_limited_response(retry_after: int) -> Response:
    return JSONResponse(
        {"error": "Too many export requests. Please try again shortly."},
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


def _export_user_library(user_id: int) -> list:
    """Shared by /api/export (self-service, one user) and /admin/export-all
    (admin, loops this over every user) — see issue #90."""
    rows = db.fetchall(
        """
        SELECT
            a.id                   AS anilist_id,
            a.title_romaji,
            a.title_english,
            a.format,
            a.episodes,
            a.season,
            a.season_year,
            a.average_score        AS anilist_score,
            a.genres,
            le.status,
            le.score               AS my_score,
            le.progress,
            le.repeat_count,
            le.start_date,
            le.finish_date,
            le.anilist_updated_at,
            pn.drop_reason,
            pn.notes,
            pn.personal_tags,
            pn.mood_tags,
            pn.watch_next_priority,
            pn.anilist_id_override,
            pn.favorite
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        LEFT JOIN personal_notes pn ON pn.anime_id = a.id AND pn.user_id = le.user_id
        WHERE le.user_id = %s
        ORDER BY le.status, a.title_romaji
        """,
        (user_id,),
    )
    export = []
    for r in rows:
        entry = dict(r)
        for field in ("start_date", "finish_date"):
            if entry.get(field):
                entry[field] = entry[field].isoformat()
        if entry.get("anilist_updated_at"):
            entry["anilist_updated_at"] = entry["anilist_updated_at"].isoformat()
        export.append(entry)
    return export


@app.get("/api/export")
def export_library(request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    allowed, retry_after = _check_export_rate_limit(f"export:{user['id']}", max_requests=10, window_seconds=60)
    if not allowed:
        return _export_rate_limited_response(retry_after)

    export = _export_user_library(user["id"])
    return Response(
        content=json.dumps(export, default=str, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=anime_library_export.json"},
    )


def _analyze_personal_notes_import(user_id: int, entries: list) -> dict:
    """Issue #87 — read-only analysis pass for POST /api/import, no writes.

    Figures out, for the given parsed /api/export JSON entries:
      - which entries actually carry personal-layer data worth importing at all
        (an export always includes every library entry, and most of them have
        every personal_notes field null — importing those as-is would either be a
        pure no-op or, worse, wipe out real notes the target account has grown
        since the export was taken, so they're excluded from `importable` here,
        never even reaching the overwrite count below)
      - which of those match a local `anime` row by anilist_id (anime.id IS the
        AniList media id in this schema — see schema.sql). A row that doesn't
        match is skipped, not an error: `anime` is sync-job-owned and this
        endpoint must never write to it, per CLAUDE.md's AniList-sourced/
        personal-layer split.
      - how many of the matched rows already have a personal_notes row for this
        user — the count the caller uses to decide whether explicit confirmation
        is required before anything is written.
    """
    candidate_ids = set()
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("anilist_id"), int):
            candidate_ids.add(e["anilist_id"])

    if candidate_ids:
        ids_list = list(candidate_ids)
        known_anime = {
            r["id"] for r in db.fetchall(
                "SELECT id FROM anime WHERE id = ANY(%s)", (ids_list,)
            )
        }
        existing_notes = {
            r["anime_id"] for r in db.fetchall(
                "SELECT anime_id FROM personal_notes WHERE user_id = %s AND anime_id = ANY(%s)",
                (user_id, ids_list),
            )
        }
    else:
        known_anime = set()
        existing_notes = set()

    importable = []
    unmatched_count = 0
    overwrite_count = 0

    for e in entries:
        if not isinstance(e, dict):
            continue
        anilist_id = e.get("anilist_id")
        if not isinstance(anilist_id, int):
            continue

        drop_reason = (e.get("drop_reason") or "").strip() or None
        notes = (e.get("notes") or "").strip() or None
        tags = e.get("personal_tags") or []
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).strip() for t in tags if str(t).strip()]
        mood = _filter_mood_tags(e.get("mood_tags") or [])
        priority = e.get("watch_next_priority")
        priority = priority if isinstance(priority, int) else None
        al_override = e.get("anilist_id_override")
        al_override = al_override if isinstance(al_override, int) else None

        has_personal_data = bool(
            drop_reason or notes or tags or mood or priority is not None or al_override is not None
        )
        if not has_personal_data:
            continue

        if anilist_id not in known_anime:
            unmatched_count += 1
            continue

        if anilist_id in existing_notes:
            overwrite_count += 1

        importable.append((anilist_id, drop_reason, tags, notes, priority, al_override, mood))

    return {
        "importable": importable,
        "unmatched_count": unmatched_count,
        "overwrite_count": overwrite_count,
    }


def _import_requires_confirmation(analysis: dict, confirm: bool) -> bool:
    """The confirmation gate itself, split out so the "would this overwrite
    anything" decision is testable independent of the HTTP layer."""
    return analysis["overwrite_count"] > 0 and not confirm


def _apply_personal_notes_import(user_id: int, importable: list) -> None:
    """Upserts personal_notes for exactly the rows _analyze_personal_notes_import
    decided are importable — same upsert shape as the single-entry /notes endpoints
    elsewhere in this file, just looped. Never touches a row that wasn't in
    `importable`, i.e. never touches a row the import JSON didn't actually carry
    personal-layer data for."""
    for anime_id, drop_reason, tags, notes, priority, al_override, mood in importable:
        db.execute(
            """
            INSERT INTO personal_notes (user_id, anime_id, drop_reason, personal_tags, mood_tags, notes, watch_next_priority, anilist_id_override)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, anime_id) DO UPDATE SET
                drop_reason = EXCLUDED.drop_reason,
                personal_tags = EXCLUDED.personal_tags,
                mood_tags = EXCLUDED.mood_tags,
                notes = EXCLUDED.notes,
                watch_next_priority = EXCLUDED.watch_next_priority,
                anilist_id_override = EXCLUDED.anilist_id_override,
                updated_at = now()
            """,
            (user_id, anime_id, drop_reason, json.dumps(tags), json.dumps(mood), notes, priority, al_override),
        )


@app.post("/api/import")
async def import_personal_data(request: Request):
    """Issue #87 — the import/restore counterpart to GET /api/export above. Reads
    the exact same JSON shape back in and upserts personal_notes for the
    requesting user. Self-service only, matching export already being
    self-service: a user can only ever import into their own account, matching
    the current session's user, never someone else's.

    Deliberately personal_notes-only — library_entries is AniList-sourced and
    rebuildable by the sync job (see CLAUDE.md); #87's acceptance criteria only
    call for restoring the personal layer, so that's exactly what this does and
    nothing more.

    Two-phase confirm: if any matched entry would overwrite an existing
    personal_notes row, nothing is written and the response instead reports how
    many rows would be overwritten, for the caller to show a confirmation prompt
    and retry with confirm=true. A fresh account (no existing personal_notes at
    all) never hits this gate — the import is written in a single call.
    """
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    entries = body.get("entries")
    confirm = bool(body.get("confirm", False))
    if not isinstance(entries, list):
        return JSONResponse({"error": "entries must be a list"}, status_code=400)

    analysis = _analyze_personal_notes_import(user["id"], entries)

    if _import_requires_confirmation(analysis, confirm):
        return JSONResponse({
            "ok": False,
            "requires_confirmation": True,
            "overwrite_count": analysis["overwrite_count"],
            "matched_count": len(analysis["importable"]),
            "unmatched_count": analysis["unmatched_count"],
        })

    _apply_personal_notes_import(user["id"], analysis["importable"])
    return JSONResponse({
        "ok": True,
        "imported": len(analysis["importable"]),
        "overwritten": analysis["overwrite_count"],
        "unmatched_count": analysis["unmatched_count"],
    })


@app.get("/admin/export-all")
def admin_export_all(request: Request):
    """Admin-only full-instance backup: zips every user's export (same query as
    /api/export, looped) into one download. Manual trigger only — see issue #90;
    deliberately not scheduled, unlike sync/recommender."""
    denied = _require_admin(request)
    if denied:
        return denied

    # Tighter budget than /api/export above: this loops the same query over every
    # user in one call, so it's the more expensive of the two per request (issue #314).
    admin = get_current_user(request)
    allowed, retry_after = _check_export_rate_limit(
        f"export-all:{admin['id']}", max_requests=3, window_seconds=300
    )
    if not allowed:
        return _export_rate_limited_response(retry_after)

    users = db.fetchall("SELECT id, email FROM users ORDER BY email")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for u in users:
            export = _export_user_library(u["id"])
            safe_email = re.sub(r"[^A-Za-z0-9_.@-]", "_", u["email"])
            zf.writestr(
                f"{u['id']}_{safe_email}.json",
                json.dumps(export, default=str, ensure_ascii=False, indent=2),
            )
    buf.seek(0)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=anidex_export_all_{timestamp}.zip"},
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    user, denied = _require_user(request)
    if denied:
        return denied

    q = q.strip()
    entries = []
    if q:
        # Issue #221: search also covers the personal-notes layer (personal_notes.notes,
        # rewatch_notes.note, episode_notes.note), not just anime titles, so a user can
        # find "that show where I wrote about X" without remembering which title it was
        # on. Plain ILIKE rather than a tsvector/GIN index — per the issue's own scope
        # note, this app's realistic per-user note volume doesn't justify the index
        # overhead, and library_entries/personal_notes are both already unique per
        # (user_id, anime_id) so the LEFT JOIN below can't fan out extra rows. Every
        # note table is filtered by the searching user's own id, never cross-user.
        # matched_title / matched_notes tell the template *why* a row matched, since a
        # notes-only match won't have the query string visible anywhere else on the card.
        pattern = f"%{q}%"
        rows = db.fetchall(
            """
            SELECT
                a.id,
                a.title_english,
                a.title_romaji,
                a.cover_image_url,
                a.format,
                a.episodes,
                a.average_score,
                a.genres,
                a.external_links,
                le.status,
                le.score,
                le.progress,
                (a.title_english ILIKE %(pattern)s
                 OR a.title_romaji ILIKE %(pattern)s
                 OR a.title_native ILIKE %(pattern)s) AS matched_title,
                (pn.notes ILIKE %(pattern)s
                 OR EXISTS (
                     SELECT 1 FROM rewatch_notes rn
                     WHERE rn.user_id = le.user_id AND rn.anime_id = a.id
                       AND rn.note ILIKE %(pattern)s
                 )
                 OR EXISTS (
                     SELECT 1 FROM episode_notes en
                     WHERE en.user_id = le.user_id AND en.anime_id = a.id
                       AND en.note ILIKE %(pattern)s
                 )) AS matched_notes
            FROM library_entries le
            JOIN anime a ON a.id = le.anime_id
            LEFT JOIN personal_notes pn ON pn.user_id = le.user_id AND pn.anime_id = a.id
            WHERE le.user_id = %(user_id)s
              AND (
                a.title_english ILIKE %(pattern)s
                OR a.title_romaji ILIKE %(pattern)s
                OR a.title_native ILIKE %(pattern)s
                OR pn.notes ILIKE %(pattern)s
                OR EXISTS (
                    SELECT 1 FROM rewatch_notes rn
                    WHERE rn.user_id = le.user_id AND rn.anime_id = a.id
                      AND rn.note ILIKE %(pattern)s
                )
                OR EXISTS (
                    SELECT 1 FROM episode_notes en
                    WHERE en.user_id = le.user_id AND en.anime_id = a.id
                      AND en.note ILIKE %(pattern)s
                )
              )
            ORDER BY le.status, a.title_romaji
            """,
            {"pattern": pattern, "user_id": user["id"]},
        )
        for row in rows:
            entry = dict(row)
            entry["streaming_links"] = [
                lnk for lnk in (row["external_links"] or [])
                if lnk.get("site") in STREAMING_SITES
            ]
            entries.append(entry)
    return templates.TemplateResponse(
        request,
        "search.html",
        {"q": q, "entries": entries},
    )


# ── Collections (issue #200) ────────────────────────────────────────────────────
# Named, saved filter combinations over the library view's existing tag/status/
# score/format/season/rewatch/sort controls — no new organizing primitive, no new
# per-anime relationship. A collection is a shortcut to a filter *state*; applying
# one just re-drives the same client-side filter controls a user would click by
# hand (see library.html/script.js), it never looks up which anime "belong" to it.
COLLECTION_FILTER_KEYS = {"format", "season", "tag", "score", "rewatch", "sort", "status", "q"}
COLLECTION_NAME_MAX_LEN = 100


def _sanitize_collection_filters(raw) -> dict:
    """Whitelist to exactly the library view's own filter/sort/search keys — a
    collection stores filter criteria only, never anime ids or anything from
    personal_notes. Every value is coerced to a stripped str since that's what the
    client-side filter functions compare against (button dataset values and select
    option values are always strings)."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in COLLECTION_FILTER_KEYS:
        val = raw.get(key)
        if val is None:
            continue
        val = str(val).strip()
        if val:
            out[key] = val
    return out


@app.get("/api/collections")
def list_collections(request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    rows = db.fetchall(
        "SELECT id, name, filters FROM collections WHERE user_id = %s ORDER BY name",
        (user["id"],),
    )
    return JSONResponse({"items": [dict(r) for r in rows]})


@app.post("/api/collections")
async def create_collection(request: Request):
    """Save the library view's current active filter/sort state as a named
    collection."""
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    if len(name) > COLLECTION_NAME_MAX_LEN:
        return JSONResponse({"error": "name too long"}, status_code=400)
    filters = _sanitize_collection_filters(body.get("filters"))

    existing = db.fetchone(
        "SELECT id FROM collections WHERE user_id = %s AND name = %s",
        (user["id"], name),
    )
    if existing:
        return JSONResponse({"error": "a collection with that name already exists"}, status_code=409)

    try:
        row = db.execute_returning(
            """
            INSERT INTO collections (user_id, name, filters)
            VALUES (%s, %s, %s::jsonb)
            RETURNING id, name, filters
            """,
            (user["id"], name, json.dumps(filters)),
        )
    except psycopg2.errors.UniqueViolation:
        # Backstop for a same-name race against the pre-check above — the UNIQUE
        # (user_id, name) constraint is the real guarantee, this just turns it
        # into the same clean 409 instead of a 500.
        return JSONResponse({"error": "a collection with that name already exists"}, status_code=409)

    return JSONResponse({"ok": True, "collection": dict(row)})


@app.patch("/api/collections/{collection_id}")
async def update_collection(collection_id: int, request: Request):
    """Rename and/or re-save the filter criteria of an existing collection —
    scoped to the owning user like every other personal-layer write."""
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    sets = []
    params = []

    if "name" in body:
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name required"}, status_code=400)
        if len(name) > COLLECTION_NAME_MAX_LEN:
            return JSONResponse({"error": "name too long"}, status_code=400)
        dup = db.fetchone(
            "SELECT id FROM collections WHERE user_id = %s AND name = %s AND id != %s",
            (user["id"], name, collection_id),
        )
        if dup:
            return JSONResponse({"error": "a collection with that name already exists"}, status_code=409)
        sets.append("name = %s")
        params.append(name)

    if "filters" in body:
        filters = _sanitize_collection_filters(body.get("filters"))
        sets.append("filters = %s::jsonb")
        params.append(json.dumps(filters))

    if not sets:
        return JSONResponse({"error": "nothing to update"}, status_code=400)

    sets.append("updated_at = now()")
    params.extend([collection_id, user["id"]])

    try:
        row = db.execute_returning(
            f"UPDATE collections SET {', '.join(sets)} WHERE id = %s AND user_id = %s "
            "RETURNING id, name, filters",
            tuple(params),
        )
    except psycopg2.errors.UniqueViolation:
        return JSONResponse({"error": "a collection with that name already exists"}, status_code=409)

    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True, "collection": dict(row)})


@app.delete("/api/collections/{collection_id}")
def delete_collection(collection_id: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    row = db.execute_returning(
        "DELETE FROM collections WHERE id = %s AND user_id = %s RETURNING id",
        (collection_id, user["id"]),
    )
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/", response_class=HTMLResponse)
def library(request: Request, response: Response, status: str = None):
    user, denied = _require_user(request)
    if denied:
        return denied

    response.headers["Cache-Control"] = "no-store"
    statuses = ["WATCHING", "COMPLETED", "DROPPED", "PLANNING", "PAUSED", "REPEATING"]
    active_status = status.upper() if status else "WATCHING"

    # "ALL" (issue #225) — not one of the six tabs rendered below, so it's only ever
    # reached via a URL a stat-card drill-down link builds directly: some /stats
    # numbers (e.g. total episodes watched, total watch time) sum across every
    # status, not just one, so no single existing status tab can show "the list that
    # sums to it". Same "ALL" pseudo-status pattern /queue already uses for its
    # PLANNING+PAUSED tab (see queue() above) — just unscoped here, skipping the
    # status filter entirely rather than swapping it for an IN (...) list.
    status_filter_sql = "" if active_status == "ALL" else "le.status = %s AND "
    where_params = (user["id"],) if active_status == "ALL" else (active_status, user["id"])

    rows = db.fetchall(
        f"""
        SELECT
            a.id,
            a.title_english,
            a.title_romaji,
            a.cover_image_url,
            a.format,
            a.episodes,
            a.average_score,
            a.genres,
            a.external_links,
            a.season,
            a.season_year,
            le.status,
            le.score,
            le.progress,
            le.repeat_count,
            le.finish_date,
            le.anilist_updated_at,
            pn.drop_reason,
            pn.personal_tags,
            pn.mood_tags,
            pn.notes,
            pn.watch_next_priority,
            pn.favorite,
            next_ep.episode AS next_episode,
            next_ep.airing_at AS next_airing_at,
            (en.note IS NOT NULL) AS has_episode_note
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        LEFT JOIN personal_notes pn ON pn.anime_id = a.id AND pn.user_id = le.user_id
        LEFT JOIN LATERAL (
            SELECT episode, airing_at
            FROM airing_schedule_cache
            WHERE anime_id = a.id AND airing_at > now()
            ORDER BY airing_at
            LIMIT 1
        ) next_ep ON true
        LEFT JOIN episode_notes en
            ON en.anime_id = a.id AND en.user_id = le.user_id AND en.episode_number = le.progress
        WHERE {status_filter_sql}le.user_id = %s
        ORDER BY le.score DESC NULLS LAST, a.title_romaji
        """,
        where_params,
    )

    stale_threshold = datetime.now(timezone.utc) - timedelta(days=60)
    tz = ZoneInfo(config.get(user["id"], "timezone") or "Europe/London")

    entries = []
    for row in rows:
        entry = dict(row)
        entry["streaming_links"] = [
            lnk for lnk in (row["external_links"] or [])
            if lnk.get("site") in STREAMING_SITES
        ]
        updated_at = entry.get("anilist_updated_at")
        entry["is_stale"] = (
            entry["status"] == "WATCHING"
            and updated_at is not None
            and updated_at < stale_threshold
        )
        airing_at = entry.get("next_airing_at")
        if airing_at:
            local = airing_at.astimezone(tz)
            now = datetime.now(timezone.utc)
            delta = airing_at - now
            days = delta.days
            if days == 0:
                entry["next_airing_label"] = "Today"
            elif days == 1:
                entry["next_airing_label"] = "Tomorrow"
            else:
                entry["next_airing_label"] = f"in {days}d"
        else:
            entry["next_airing_label"] = None
        entries.append(entry)

    collection_rows = db.fetchall(
        "SELECT id, name, filters FROM collections WHERE user_id = %s ORDER BY name",
        (user["id"],),
    )
    # Same pattern as base.html's window.I18N (issue #147): a small JSON payload
    # embedded via a `<script>` + `|safe`, with `<` escaped so a collection name
    # containing e.g. "</script>" can never break out of the block it's embedded
    # in. Jinja's normal HTML-attribute autoescaping doesn't apply inside a
    # <script> body, so this can't rely on that the way an ordinary `{{ }}` would.
    collections_json = json.dumps(
        [{"id": c["id"], "name": c["name"], "filters": c["filters"]} for c in collection_rows],
        ensure_ascii=False,
    ).replace("<", "\\u003c")

    # Issue #235 — "what's new since your last visit" in-app digest. `/` is the
    # redirect target of a real login and the natural landing page of a return
    # visit, so this is the one deliberate call site — see
    # _whats_new_for_request's docstring for why this isn't in the shared
    # _nav_context processor instead.
    whats_new = _whats_new_for_request(request, user, _get_current_impersonation(request))

    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "entries": entries,
            "statuses": statuses,
            "active_status": active_status,
            "collections_json": collections_json,
            "nav_whats_new": whats_new,
        },
    )


@app.post("/api/anime/{anime_id}/rating")
async def set_rating(anime_id: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    stars = int(body.get("score", 0))
    if stars < 0 or stars > 5:
        return JSONResponse({"error": "score must be 0–5"}, status_code=400)

    error = _apply_rating_change(user, anime_id, stars)
    if error:
        status_code = 500 if error == "AniList token not configured" else 502
        return JSONResponse({"error": error}, status_code=status_code)

    return JSONResponse({"ok": True, "score": stars})


def _apply_rating_change(user, anime_id: int, stars: int) -> str | None:
    """Push a 0–5 star rating to AniList (unless mocked) then mirror it into
    library_entries.score locally. Returns an error message on failure, None on
    success — same shape as _apply_status_change/_apply_progress_change below
    (the literal string "AniList token not configured" is what callers
    string-match on to pick 500 vs 502, same convention both of those already
    use). The actual write logic behind /api/anime/{id}/rating above, reused
    as-is by the MCP set_rating write tool (issue #208)."""
    # AniList account uses POINT_5 format — send 0–5 directly.
    # Reading back via sync uses score(format: POINT_100), which AniList converts correctly.
    anilist_score = float(stars)

    if not ANILIST_MOCK:
        token = _get_anilist_token(user["id"])
        if not token:
            return "AniList token not configured"

        try:
            resp = httpx.post(
                ANILIST_API,
                json={"query": SAVE_SCORE_MUTATION, "variables": {"mediaId": anime_id, "score": anilist_score}},
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                log.error("AniList rating error for %s: %s", anime_id, data["errors"])
                return str(data["errors"])
            saved = ((data.get("data") or {}).get("SaveMediaListEntry")) or {}
            if not saved:
                log.error("AniList rating: SaveMediaListEntry returned null for mediaId=%s", anime_id)
                return "AniList returned null — entry may not be in your list"
            returned_score = saved.get("score")
            if returned_score != anilist_score:
                log.warning(
                    "AniList score mismatch for %s: sent %s, got back %s",
                    anime_id, anilist_score, returned_score,
                )
        except Exception as e:
            log.error("AniList rating request failed for %s: %s", anime_id, e)
            return str(e)

    local_score = stars if stars > 0 else None
    db.execute(
        "UPDATE library_entries SET score = %s WHERE anime_id = %s AND user_id = %s",
        (local_score, anime_id, user["id"]),
    )

    return None


@app.post("/api/anime/{anime_id}/favorite")
async def set_favorite(anime_id: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    favorite = bool(body.get("favorite"))

    _set_favorite(user["id"], anime_id, favorite)
    return JSONResponse({"ok": True, "favorite": favorite})


def _set_favorite(user_id: int, anime_id: int, favorite: bool) -> None:
    """Mark/unmark an anime as a personal favorite (issue #219) — Letterboxd's
    heart-vs-star pattern, a nullable boolean signal on personal_notes,
    independent of library_entries.score. Purely local: never pushed to AniList
    (the app's only AniList mutations are rating/status/progress, see CLAUDE.md's
    guardrail — this is deliberately not a fourth one).

    Dedicated upsert touching only the favorite column, not routed through
    _upsert_personal_notes' full-replace semantics (which overwrites drop_reason/
    personal_tags/notes/watch_next_priority/anilist_id_override with exactly what
    the caller passes) — same reasoning as _apply_rating_change owning its own
    single-column UPDATE rather than going through that full-replace path.
    Toggling the heart from a library card or the notes page must never clobber
    those other fields, and vice versa."""
    db.execute(
        """
        INSERT INTO personal_notes (user_id, anime_id, favorite)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, anime_id) DO UPDATE SET
            favorite = EXCLUDED.favorite,
            updated_at = now()
        """,
        (user_id, anime_id, favorite),
    )


def _apply_status_change(user, anime_id: int, status: str) -> str | None:
    """Push status to AniList (unless mocked) and upsert library_entries.
    Returns an error message on failure, None on success."""
    # Issue #287 — captured before the upsert below so the post-write check can tell
    # a genuine transition INTO Planning (this row's prior status, if any, wasn't
    # already PLANNING) apart from a no-op re-post of the same status, which must
    # never re-fire the notification.
    prev = db.fetchone(
        "SELECT status FROM library_entries WHERE user_id = %s AND anime_id = %s",
        (user["id"], anime_id),
    )
    prev_status = prev["status"] if prev else None

    anilist_status = STATUS_TO_ANILIST.get(status, status)

    if not ANILIST_MOCK:
        token = _get_anilist_token(user["id"])
        if not token:
            return "AniList token not configured"

        try:
            resp = httpx.post(
                ANILIST_API,
                json={"query": SAVE_STATUS_MUTATION, "variables": {"mediaId": anime_id, "status": anilist_status}},
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                return str(data["errors"])
        except Exception as e:
            return str(e)

    db.execute(
        """
        INSERT INTO library_entries (user_id, anime_id, status, sync_status)
        VALUES (%s, %s, %s, 'synced')
        ON CONFLICT (user_id, anime_id) DO UPDATE SET status = EXCLUDED.status, sync_status = 'synced'
        """,
        (user["id"], anime_id, status),
    )
    # This push just succeeded synchronously, so it's authoritative — clear any
    # outbox row left over from an earlier bulk edit on this anime (e.g. a failed
    # bulk edit fixed by hand via the single-card UI). Without this, a stale
    # outbox row could later get replayed via "Retry failed" and silently
    # overwrite the status just set here.
    db.execute(
        "DELETE FROM status_sync_outbox WHERE user_id = %s AND anime_id = %s",
        (user["id"], anime_id),
    )

    if status == "PLANNING" and prev_status != "PLANNING":
        try:
            _notify_if_planning_uncovered(user["id"], anime_id)
        except Exception as e:
            log.error("Planning-coverage notification failed for user %s anime %s: %s", user["id"], anime_id, e)

    return None


@app.post("/api/anime/{anime_id}/status")
async def set_status(anime_id: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    status = body.get("status", "").upper()
    if status not in VALID_STATUSES:
        return JSONResponse({"error": "invalid status"}, status_code=400)

    error = _apply_status_change(user, anime_id, status)
    if error:
        status_code = 500 if error == "AniList token not configured" else 502
        return JSONResponse({"error": error}, status_code=status_code)

    return JSONResponse({"ok": True, "status": status})


@app.post("/api/anime/bulk-status")
async def bulk_set_status(request: Request):
    """Local-first bulk status edit (issue #18): writes land immediately and AniList
    sync happens async via the outbox worker, unlike the single-card endpoint above."""
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    status = body.get("status", "").upper()
    anime_ids = body.get("anime_ids", [])
    if status not in VALID_STATUSES:
        return JSONResponse({"error": "invalid status"}, status_code=400)
    if not anime_ids or not all(isinstance(i, int) for i in anime_ids):
        return JSONResponse({"error": "anime_ids required"}, status_code=400)

    # Issue #287 — same "was this row already PLANNING" guard _apply_status_change
    # uses for the single-card endpoint, captured up front here since this endpoint
    # writes many rows in one request.
    prev_statuses = {
        r["anime_id"]: r["status"] for r in db.fetchall(
            "SELECT anime_id, status FROM library_entries WHERE user_id = %s AND anime_id = ANY(%s)",
            (user["id"], anime_ids),
        )
    }

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for anime_id in anime_ids:
                cur.execute(
                    """
                    INSERT INTO library_entries (user_id, anime_id, status, sync_status)
                    VALUES (%s, %s, %s, 'pending')
                    ON CONFLICT (user_id, anime_id) DO UPDATE SET
                        status = EXCLUDED.status, sync_status = 'pending'
                    """,
                    (user["id"], anime_id, status),
                )
                # Supersede any not-yet-processed outbox row for this anime so the
                # worker doesn't redundantly push a now-stale target status.
                cur.execute(
                    """
                    DELETE FROM status_sync_outbox
                    WHERE user_id = %s AND anime_id = %s AND state IN ('pending', 'failed')
                    """,
                    (user["id"], anime_id),
                )
                cur.execute(
                    "INSERT INTO status_sync_outbox (user_id, anime_id, source, status) VALUES (%s, %s, 'ui_bulk_edit', %s)",
                    (user["id"], anime_id, status),
                )
        conn.commit()

    outbox.wake()

    if status == "PLANNING":
        for anime_id in anime_ids:
            if prev_statuses.get(anime_id) != "PLANNING":
                try:
                    _notify_if_planning_uncovered(user["id"], anime_id)
                except Exception as e:
                    log.error("Planning-coverage notification failed for user %s anime %s: %s", user["id"], anime_id, e)

    return JSONResponse({"ok": True, "count": len(anime_ids)})


@app.get("/api/anime/bulk-status")
def bulk_status(request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    ids_param = request.query_params.get("anime_ids", "")
    anime_ids = [int(i) for i in ids_param.split(",") if i.strip().isdigit()]
    if not anime_ids:
        return JSONResponse({"items": []})

    rows = db.fetchall(
        """
        SELECT anime_id, state, last_error FROM status_sync_outbox
        WHERE user_id = %s AND anime_id = ANY(%s)
        """,
        (user["id"], anime_ids),
    )
    by_id = {r["anime_id"]: r for r in rows}
    items = [
        {
            "anime_id": aid,
            # not present in by_id => already synced (row deleted on success) or never queued
            "state": by_id[aid]["state"] if aid in by_id else "synced",
            "error": by_id[aid]["last_error"] if aid in by_id else None,
        }
        for aid in anime_ids
    ]
    return JSONResponse({"items": items})


@app.post("/api/anime/{anime_id}/bulk-status/retry")
async def bulk_status_retry(anime_id: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    row = db.execute_returning(
        """
        UPDATE status_sync_outbox SET state = 'pending', attempts = 0, last_error = NULL, updated_at = now()
        WHERE user_id = %s AND anime_id = %s AND state = 'failed'
        RETURNING id
        """,
        (user["id"], anime_id),
    )
    if not row:
        return JSONResponse({"error": "no failed item for this anime"}, status_code=404)

    outbox.wake()
    return JSONResponse({"ok": True})


@app.post("/api/anime/bulk-tags")
async def bulk_add_tags(request: Request):
    """Bulk-apply personal tags from library bulk-select mode (issue #15). Additive —
    merges into each entry's existing personal_tags rather than replacing them, and
    purely local (personal_notes only, no AniList mutation), so it commits
    synchronously instead of going through the status_sync_outbox like bulk-status."""
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    tags_raw = body.get("tags") or ""
    anime_ids = body.get("anime_ids", [])
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    if not tags:
        return JSONResponse({"error": "tags required"}, status_code=400)
    if not anime_ids or not all(isinstance(i, int) for i in anime_ids):
        return JSONResponse({"error": "anime_ids required"}, status_code=400)

    count = _bulk_apply_tags(user["id"], anime_ids, tags)
    return JSONResponse({"ok": True, "count": count})


def _bulk_apply_tags(user_id: int, anime_ids: list, tags: list) -> int:
    """Additive, case-insensitive-deduped tag merge across an explicit list of
    anime_ids — the actual write logic behind /api/anime/bulk-tags above (issue
    #15), reused as-is by the MCP bulk_apply_tags write tool (issue #208).
    anime_ids must already be a concrete list of ids by the time this is called;
    there is no filter/query form of this operation anywhere in this app."""
    tags_json = json.dumps(tags)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for anime_id in anime_ids:
                cur.execute(
                    """
                    INSERT INTO personal_notes (user_id, anime_id, personal_tags)
                    VALUES (%s, %s, %s::jsonb)
                    ON CONFLICT (user_id, anime_id) DO UPDATE SET
                        personal_tags = (
                            -- Dedupe case-insensitively (matches how privacy.py/
                            -- run_recommender.py compare tags), keeping each tag's
                            -- first-seen casing via ordinality.
                            SELECT COALESCE(jsonb_agg(tag ORDER BY ord), '[]'::jsonb)
                            FROM (
                                SELECT DISTINCT ON (lower(tag)) tag, ord
                                FROM jsonb_array_elements_text(
                                    personal_notes.personal_tags || EXCLUDED.personal_tags
                                ) WITH ORDINALITY AS t(tag, ord)
                                ORDER BY lower(tag), ord
                            ) deduped
                        ),
                        updated_at = now()
                    """,
                    (user_id, anime_id, tags_json),
                )
        conn.commit()

    return len(anime_ids)


@app.post("/api/anime/{anime_id}/progress")
async def set_progress(anime_id: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    progress = body.get("progress")
    if not isinstance(progress, int) or progress < 0:
        return JSONResponse({"error": "progress must be a non-negative integer"}, status_code=400)

    error = _apply_progress_change(user, anime_id, progress)
    if error:
        status_code = 500 if error == "AniList token not configured" else 502
        return JSONResponse({"error": error}, status_code=status_code)
    return JSONResponse({"ok": True, "progress": progress})


def _apply_progress_change(user, anime_id: int, progress: int) -> str | None:
    """Push episode progress to AniList (unless mocked) then mirror it into
    library_entries locally. Returns an error message on failure, None on
    success. The actual write logic behind /api/anime/{id}/progress above,
    reused as-is by the MCP set_progress write tool (issue #208)."""
    if not ANILIST_MOCK:
        token = _get_anilist_token(user["id"])
        if not token:
            return "AniList token not configured"

        try:
            resp = httpx.post(
                ANILIST_API,
                json={"query": SAVE_PROGRESS_MUTATION, "variables": {"mediaId": anime_id, "progress": progress}},
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                return str(data["errors"])
        except Exception as e:
            return str(e)

    db.execute(
        "UPDATE library_entries SET progress = %s WHERE anime_id = %s AND user_id = %s",
        (progress, anime_id, user["id"]),
    )
    return None


@app.post("/api/anime/{anime_id}/delete")
async def delete_anime(anime_id: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    token = _get_anilist_token(user["id"])
    if not token:
        return JSONResponse({"error": "AniList token not configured"}, status_code=500)

    row = db.fetchone(
        "SELECT anilist_entry_id FROM library_entries WHERE anime_id = %s AND user_id = %s",
        (anime_id, user["id"]),
    )
    if not row:
        return JSONResponse({"error": "not in library"}, status_code=404)

    entry_id = row["anilist_entry_id"]
    if not entry_id:
        # Not yet backfilled by a sync run (e.g. added moments ago) — resolve it live.
        try:
            resp = httpx.post(
                ANILIST_API,
                json={"query": MEDIA_LIST_ENTRY_ID_QUERY, "variables": {"mediaId": anime_id}},
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                return JSONResponse({"error": str(data["errors"])}, status_code=502)
            media = (data.get("data") or {}).get("Media") or {}
            entry_id = (media.get("mediaListEntry") or {}).get("id")
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    if not entry_id:
        return JSONResponse({"error": "could not resolve AniList list entry"}, status_code=502)

    try:
        del_resp = httpx.post(
            ANILIST_API,
            json={"query": DELETE_MEDIA_LIST_ENTRY_MUTATION, "variables": {"id": entry_id}},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            timeout=10,
        )
        del_resp.raise_for_status()
        del_data = del_resp.json()
        if "errors" in del_data:
            log.error("AniList delete error for user=%s anime_id=%s entry_id=%s: %s",
                       user["id"], anime_id, entry_id, del_data["errors"])
            return JSONResponse({"error": str(del_data["errors"])}, status_code=502)
        deleted = ((del_data.get("data") or {}).get("DeleteMediaListEntry")) or {}
        if not deleted.get("deleted"):
            log.error("AniList delete: not confirmed for user=%s anime_id=%s entry_id=%s",
                       user["id"], anime_id, entry_id)
            return JSONResponse({"error": "AniList did not confirm deletion"}, status_code=502)
    except Exception as e:
        log.error("AniList delete request failed for user=%s anime_id=%s: %s", user["id"], anime_id, e)
        return JSONResponse({"error": str(e)}, status_code=502)

    # personal_notes / recommendation_scores are deliberately left in place — if the
    # anime gets re-added later, drop reasons/tags/notes should still be there.
    db.execute(
        "DELETE FROM library_entries WHERE anime_id = %s AND user_id = %s",
        (anime_id, user["id"]),
    )
    return JSONResponse({"ok": True})


@app.post("/api/queue/reorder")
async def reorder_queue(request: Request):
    """Accept [{anime_id, priority}] and bulk-update personal_notes.watch_next_priority."""
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    items = body if isinstance(body, list) else []
    for item in items:
        anime_id = int(item["anime_id"])
        priority = item.get("priority")
        db.execute(
            """
            INSERT INTO personal_notes (user_id, anime_id, watch_next_priority)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, anime_id) DO UPDATE SET watch_next_priority = EXCLUDED.watch_next_priority
            """,
            (user["id"], anime_id, priority),
        )
    return JSONResponse({"ok": True})


@app.get("/api/search/anilist")
async def search_anilist(request: Request, q: str = ""):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    q = q.strip()
    if len(q) < 2:
        return JSONResponse([])

    try:
        resp = httpx.post(
            ANILIST_API,
            json={"query": ANILIST_SEARCH_QUERY, "variables": {"search": q}},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    media_list = ((data.get("data") or {}).get("Page") or {}).get("media") or []

    ids = [m["id"] for m in media_list]
    in_library: dict[int, str] = {}
    if ids:
        rows = db.fetchall(
            "SELECT anime_id, status FROM library_entries WHERE anime_id = ANY(%s) AND user_id = %s",
            (ids, user["id"]),
        )
        in_library = {r["anime_id"]: r["status"] for r in rows}

    results = []
    for m in media_list:
        lib_status = in_library.get(m["id"])
        results.append({
            "id": m["id"],
            "title_english": m["title"].get("english"),
            "title_romaji": m["title"].get("romaji"),
            "format": m.get("format"),
            "season_year": m.get("seasonYear"),
            "average_score": m.get("averageScore"),
            "cover": (m.get("coverImage") or {}).get("large"),
            "in_library": lib_status is not None,
            "library_status": lib_status,
        })

    return JSONResponse(results)


@app.post("/api/anime/{anime_id}/add")
async def add_anime(anime_id: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    status = body.get("status", "PLANNING").upper()
    if status not in VALID_STATUSES:
        return JSONResponse({"error": "invalid status"}, status_code=400)

    # Issue #287 — same "was this row already PLANNING" guard as the other two
    # status-writing endpoints; re-adding an already-tracked title at the same
    # status must not re-fire the notification.
    prev = db.fetchone(
        "SELECT status FROM library_entries WHERE user_id = %s AND anime_id = %s",
        (user["id"], anime_id),
    )
    prev_status = prev["status"] if prev else None

    token = _get_anilist_token(user["id"])
    if not token:
        return JSONResponse({"error": "AniList token not configured"}, status_code=500)

    # anime is a shared/global table — if another user's sync already wrote a
    # sufficiently fresh row for this anime, reuse it instead of re-fetching
    # metadata AniList would just hand back unchanged.
    cached = db.fetchone(
        "SELECT title_english, title_romaji FROM anime "
        "WHERE id = %s AND last_synced_at > now() - INTERVAL '24 hours'",
        (anime_id,),
    )
    media = None
    if cached:
        title_english = cached["title_english"]
        title_romaji = cached["title_romaji"]
    else:
        try:
            resp = httpx.post(
                ANILIST_API,
                json={"query": ANILIST_MEDIA_QUERY, "variables": {"id": anime_id}},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                return JSONResponse({"error": str(data["errors"])}, status_code=502)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

        media = (data.get("data") or {}).get("Media")
        if not media:
            return JSONResponse({"error": "media not found"}, status_code=404)
        title_english = media["title"].get("english")
        title_romaji = media["title"].get("romaji")

    anilist_status = STATUS_TO_ANILIST.get(status, status)
    try:
        al_resp = httpx.post(
            ANILIST_API,
            json={"query": SAVE_STATUS_MUTATION, "variables": {"mediaId": anime_id, "status": anilist_status}},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            timeout=10,
        )
        al_resp.raise_for_status()
        al_data = al_resp.json()
        if "errors" in al_data:
            return JSONResponse({"error": str(al_data["errors"])}, status_code=502)
        saved = ((al_data.get("data") or {}).get("SaveMediaListEntry")) or {}
        entry_id = saved.get("id")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    if media:
        _upsert_anime_row(media)
    db.execute(
        """
        INSERT INTO library_entries (user_id, anime_id, anilist_entry_id, status, synced_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (user_id, anime_id) DO UPDATE SET
            anilist_entry_id = EXCLUDED.anilist_entry_id,
            status           = EXCLUDED.status,
            synced_at        = now()
        """,
        (user["id"], anime_id, entry_id, status),
    )

    if status == "PLANNING" and prev_status != "PLANNING":
        try:
            _notify_if_planning_uncovered(user["id"], anime_id)
        except Exception as e:
            log.error("Planning-coverage notification failed for user %s anime %s: %s", user["id"], anime_id, e)

    return JSONResponse({
        "ok": True,
        "id": anime_id,
        "title": title_english or title_romaji,
        "status": status,
    })
