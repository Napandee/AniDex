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
import zipfile
from collections import Counter
from datetime import datetime, timezone, timedelta
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
from fastapi import BackgroundTasks, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

log = logging.getLogger("anime_tracker")

load_dotenv()

from app import db, config, privacy, outbox, i18n, sessions
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
    _scheduler.add_job(
        _weekly_airing_digest,
        CronTrigger(day_of_week="mon", hour=7, minute=0, timezone="UTC"),
        id="weekly_digest", replace_existing=True,
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

STREAMING_SITES = {
    "Crunchyroll", "Netflix", "Hulu", "Amazon Prime Video", "HIDIVE",
    "Disney Plus", "Bilibili TV", "Bilibili", "iQ", "WeTV", "Tubi TV",
    "Adult Swim", "Hoopla", "Max", "Tencent Video", "Bandai Channel",
    "Niconico Video", "Funimation", "VRV",
}

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
templates = Jinja2Templates(directory="app/templates")

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


@app.on_event("shutdown")
def shutdown() -> None:
    outbox.stop_worker()
    _scheduler.shutdown(wait=False)
    log.info("APScheduler stopped")


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
    """
    if hasattr(request.state, "_cached_user"):
        return request.state._cached_user

    user = None
    token = request.session.get("sid")
    if token:
        user_id = sessions.resolve_session(token)
        if user_id:
            user = db.fetchone("SELECT * FROM users WHERE id = %s", (user_id,))
            if user and not user["is_active"]:
                sessions.revoke_session_by_token(token)
                user = None

    if user is None:
        pending_2fa = request.session.get(_PENDING_2FA_SESSION_KEY)
        request.session.clear()
        if pending_2fa is not None:
            request.session[_PENDING_2FA_SESSION_KEY] = pending_2fa

    request.state._cached_user = user
    return user


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
    user_language = config.get(user["id"], "language") if user else None
    locale = i18n.resolve_locale(request.headers.get("accept-language"), user_language)
    theme = config.get(user["id"], "theme") if user else "system"
    # `<` escaped so a translated string can never accidentally close the <script>
    # tag it's embedded in (see base.html's window.I18N assignment, #147).
    i18n_json = json.dumps(i18n.all_strings(locale), ensure_ascii=False).replace("<", "\\u003c")
    return {
        "nav_user": user,
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
        invite = db.fetchone(
            "SELECT * FROM invites WHERE email = %s AND accepted_at IS NULL",
            (email,),
        )
        if not invite:
            return None, HTMLResponse(
                "<h1>Not invited</h1>"
                f"<p>{html.escape(email)} hasn't been invited to this instance. "
                "Ask the admin to add you.</p>",
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
    valid = bool(code) and pyotp.TOTP(user["totp_secret"]).verify(code, valid_window=1)
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


def _instance_health() -> dict:
    """Read-only instance-health data for the admin panel (issue #86): running
    build version (if baked into the image via the Dockerfile's GIT_SHA build
    arg — see Dockerfile), Postgres database size, and row counts for a few key
    tables. Display only — deliberately no write/control actions here."""
    git_sha = os.environ.get("GIT_SHA", "").strip()

    db_size_row = db.fetchone(
        "SELECT pg_size_pretty(pg_database_size(current_database())) AS size"
    )

    return {
        "build_version": git_sha[:12] if git_sha else None,
        "db_size": db_size_row["size"] if db_size_row else None,
        "row_counts": {
            "library_entries": db.fetchone("SELECT COUNT(*) AS n FROM library_entries")["n"],
            "anime": db.fetchone("SELECT COUNT(*) AS n FROM anime")["n"],
            "users": db.fetchone("SELECT COUNT(*) AS n FROM users")["n"],
        },
    }


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, saved: str = ""):
    denied = _require_admin(request)
    if denied:
        return denied

    instance_health = _instance_health()

    # Sync schedule (issue #96) — moved here from Settings since it's instance-wide,
    # not per-user. Same instance_config fields _apply_schedule() reads.
    schedule = {
        "sync_daily_time": _instance_config_get("sync_daily_time") or "04:30",
        "sync_recommender_day": _instance_config_get("sync_recommender_day") or "sun",
        "sync_recommender_time": _instance_config_get("sync_recommender_time") or "05:00",
    }

    invites = db.fetchall("SELECT * FROM invites ORDER BY created_at DESC")

    now = datetime.now(timezone.utc)
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
            "invites": invites,
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


def _fetch_visible_recommendations(user_id: int) -> list[dict]:
    """Recommendation rows visible to `user_id`: not permanently dismissed, and not
    currently snoozed (issue #75). Broken out of recommendations() so the exclusion
    logic is directly testable against a real Postgres without needing to fake a
    full Request/session — see tests/test_recommendation_snooze.py.

    Issue #13: `rs.source` distinguishes the original similarity-based path from
    the new seasonal discovery digest. The LIMIT is applied per-source (via
    ROW_NUMBER) rather than globally, so a season with a lot of new releases can't
    starve out the similarity picks (or vice versa) — both get their own top-100
    budget, sorted by score within each."""
    return db.fetchall(
        """
        SELECT id, title_english, title_romaji, cover_image_url, format, episodes,
               average_score, genres, season, season_year, rec_score, reason, source
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
                ROW_NUMBER() OVER (PARTITION BY rs.source ORDER BY rs.score DESC) AS rn
            FROM recommendation_scores rs
            JOIN anime a ON a.id = rs.anime_id
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
        entries.append(entry)

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
         "rewatch_notes": rewatch_notes},
    )


@app.post("/anime/{anime_id}/notes")
def save_notes(
    request: Request,
    anime_id: int,
    drop_reason: str = Form(""),
    notes: str = Form(""),
    personal_tags: str = Form(""),
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
    try:
        priority = int(watch_next_priority.strip()) if watch_next_priority.strip() else None
    except ValueError:
        priority = None
    try:
        al_override = int(anilist_id_override.strip()) if anilist_id_override.strip() else None
    except ValueError:
        al_override = None

    db.execute(
        """
        INSERT INTO personal_notes (user_id, anime_id, drop_reason, personal_tags, notes, watch_next_priority, anilist_id_override)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, anime_id) DO UPDATE SET
            drop_reason = EXCLUDED.drop_reason,
            personal_tags = EXCLUDED.personal_tags,
            notes = EXCLUDED.notes,
            watch_next_priority = EXCLUDED.watch_next_priority,
            anilist_id_override = EXCLUDED.anilist_id_override,
            updated_at = now()
        """,
        (user["id"], anime_id, drop_reason_val, json.dumps(tags), notes_val, priority, al_override),
    )

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

    db.execute(
        """
        INSERT INTO personal_notes (user_id, anime_id, drop_reason, personal_tags, notes, watch_next_priority, anilist_id_override)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, anime_id) DO UPDATE SET
            drop_reason = EXCLUDED.drop_reason,
            personal_tags = EXCLUDED.personal_tags,
            notes = EXCLUDED.notes,
            watch_next_priority = EXCLUDED.watch_next_priority,
            anilist_id_override = EXCLUDED.anilist_id_override,
            updated_at = now()
        """,
        (user["id"], anime_id, drop_reason_val, json.dumps(tags), notes_val, priority, al_override),
    )
    return JSONResponse({"ok": True})


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


_RELATION_ORDER = ["PREQUEL", "SEQUEL", "PARENT", "SIDE_STORY", "SPIN_OFF",
                   "ALTERNATIVE", "COMPILATION", "CONTAINS", "SUMMARY", "OTHER"]


@app.get("/upcoming", response_class=HTMLResponse)
def upcoming(request: Request):
    user, denied = _require_user(request)
    if denied:
        return denied

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

    # Weekly Mon-Sun broadcast-calendar grid — groups the same entries by
    # broadcast day of week (local time), reusing airing_schedule_cache data
    # already fetched above. No change to how that data is synced/cached.
    week_grid = [
        {"name": name, "entries": []}
        for name in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    ]
    for entry in entries:
        week_grid[entry["airing_local"].weekday()]["entries"].append(entry)
    today_weekday = now.astimezone(tz).weekday()
    for idx, day in enumerate(week_grid):
        day["is_today"] = idx == today_weekday

    return templates.TemplateResponse(
        request,
        "upcoming.html",
        {"entries": entries, "week_grid": week_grid},
    )


COMMON_TIMEZONES = [
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Stockholm",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Sao_Paulo", "Asia/Tokyo", "Asia/Seoul", "Asia/Shanghai",
    "Asia/Singapore", "Asia/Kolkata", "Australia/Sydney", "Pacific/Auckland",
    "UTC",
]


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
        entries.append(entry)

    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "entries": entries,
            "queue_statuses": queue_statuses,
            "active_status": active_status,
        },
    )


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

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": current,
            "timezones": COMMON_TIMEZONES,
            "languages": i18n.SUPPORTED_LOCALES,
            "language_labels": i18n.LOCALE_LABELS,
            "last_synced": last_synced,
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

    revoked_token = sessions.revoke_session(session_id, user["id"])
    if revoked_token and revoked_token == request.session.get("sid"):
        request.session.clear()
        return RedirectResponse(url="/auth/login", status_code=303)
    if revoked_token:
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


@app.post("/settings/credentials")
def settings_save_credentials(
    request: Request,
    anilist_username: str = Form(""),
    anilist_token: str = Form(""),
    cr_etp_rt: str = Form(""),
    netflix_cookie_header: str = Form(""),
    netflix_profile_guid: str = Form(""),
):
    user, denied = _require_user(request)
    if denied:
        return denied

    config.set_value(user["id"], "anilist_username", anilist_username.strip())

    # Only overwrite token if a non-empty value was submitted (empty = leave unchanged)
    if anilist_token.strip():
        config.set_value(user["id"], "anilist_token", anilist_token.strip())
    if cr_etp_rt.strip():
        config.set_value(user["id"], "cr_etp_rt", cr_etp_rt.strip())
    if netflix_cookie_header.strip():
        config.set_value(user["id"], "netflix_cookie_header", netflix_cookie_header.strip())
    if netflix_profile_guid.strip():
        config.set_value(user["id"], "netflix_profile_guid", netflix_profile_guid.strip())

    return RedirectResponse(url="/settings?saved=credentials", status_code=303)


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
                (secret, user["id"]),
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
    return JSONResponse({
        "status": [{"label": r["status"].title(), "value": r["cnt"]} for r in status_rows],
        "scores": [{"score": r["score"], "count": r["cnt"]} for r in score_rows],
        "genres": genres_out,
        "top_genre": genres_out[0]["genre"] if genres_out else None,
        "by_year": [{"year": r["year"], "count": r["cnt"]} for r in year_rows],
        "heatmap": [{"date": r["day"].isoformat(), "count": int(r["cnt"])} for r in heatmap_rows],
        "drop_patterns": drop_patterns,
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
    })


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    user, denied = _require_user(request)
    if denied:
        return denied
    return templates.TemplateResponse(request, "stats.html")


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
            pn.watch_next_priority,
            pn.anilist_id_override
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
        priority = e.get("watch_next_priority")
        priority = priority if isinstance(priority, int) else None
        al_override = e.get("anilist_id_override")
        al_override = al_override if isinstance(al_override, int) else None

        has_personal_data = bool(
            drop_reason or notes or tags or priority is not None or al_override is not None
        )
        if not has_personal_data:
            continue

        if anilist_id not in known_anime:
            unmatched_count += 1
            continue

        if anilist_id in existing_notes:
            overwrite_count += 1

        importable.append((anilist_id, drop_reason, tags, notes, priority, al_override))

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
    for anime_id, drop_reason, tags, notes, priority, al_override in importable:
        db.execute(
            """
            INSERT INTO personal_notes (user_id, anime_id, drop_reason, personal_tags, notes, watch_next_priority, anilist_id_override)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, anime_id) DO UPDATE SET
                drop_reason = EXCLUDED.drop_reason,
                personal_tags = EXCLUDED.personal_tags,
                notes = EXCLUDED.notes,
                watch_next_priority = EXCLUDED.watch_next_priority,
                anilist_id_override = EXCLUDED.anilist_id_override,
                updated_at = now()
            """,
            (user_id, anime_id, drop_reason, json.dumps(tags), notes, priority, al_override),
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
                le.progress
            FROM library_entries le
            JOIN anime a ON a.id = le.anime_id
            WHERE
                (a.title_english ILIKE %s
                 OR a.title_romaji ILIKE %s
                 OR a.title_native ILIKE %s)
                AND le.user_id = %s
            ORDER BY le.status, a.title_romaji
            """,
            (pattern, pattern, pattern, user["id"]),
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


@app.get("/", response_class=HTMLResponse)
def library(request: Request, response: Response, status: str = None):
    user, denied = _require_user(request)
    if denied:
        return denied

    response.headers["Cache-Control"] = "no-store"
    statuses = ["WATCHING", "COMPLETED", "DROPPED", "PLANNING", "PAUSED", "REPEATING"]
    active_status = status.upper() if status else "WATCHING"

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
            pn.notes,
            pn.watch_next_priority,
            next_ep.episode AS next_episode,
            next_ep.airing_at AS next_airing_at
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
        WHERE le.status = %s AND le.user_id = %s
        ORDER BY le.score DESC NULLS LAST, a.title_romaji
        """,
        (active_status, user["id"]),
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

    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "entries": entries,
            "statuses": statuses,
            "active_status": active_status,
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

    # AniList account uses POINT_5 format — send 0–5 directly.
    # Reading back via sync uses score(format: POINT_100), which AniList converts correctly.
    anilist_score = float(stars)

    if not ANILIST_MOCK:
        token = _get_anilist_token(user["id"])
        if not token:
            return JSONResponse({"error": "AniList token not configured"}, status_code=500)

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
                return JSONResponse({"error": str(data["errors"])}, status_code=502)
            saved = ((data.get("data") or {}).get("SaveMediaListEntry")) or {}
            if not saved:
                log.error("AniList rating: SaveMediaListEntry returned null for mediaId=%s", anime_id)
                return JSONResponse({"error": "AniList returned null — entry may not be in your list"}, status_code=502)
            returned_score = saved.get("score")
            if returned_score != anilist_score:
                log.warning(
                    "AniList score mismatch for %s: sent %s, got back %s",
                    anime_id, anilist_score, returned_score,
                )
        except Exception as e:
            log.error("AniList rating request failed for %s: %s", anime_id, e)
            return JSONResponse({"error": str(e)}, status_code=502)

    local_score = stars if stars > 0 else None
    db.execute(
        "UPDATE library_entries SET score = %s WHERE anime_id = %s AND user_id = %s",
        (local_score, anime_id, user["id"]),
    )

    return JSONResponse({"ok": True, "score": stars})


def _apply_status_change(user, anime_id: int, status: str) -> str | None:
    """Push status to AniList (unless mocked) and upsert library_entries.
    Returns an error message on failure, None on success."""
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
                    (user["id"], anime_id, tags_json),
                )
        conn.commit()

    return JSONResponse({"ok": True, "count": len(anime_ids)})


@app.post("/api/anime/{anime_id}/progress")
async def set_progress(anime_id: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    progress = body.get("progress")
    if not isinstance(progress, int) or progress < 0:
        return JSONResponse({"error": "progress must be a non-negative integer"}, status_code=400)

    if not ANILIST_MOCK:
        token = _get_anilist_token(user["id"])
        if not token:
            return JSONResponse({"error": "AniList token not configured"}, status_code=500)

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
                return JSONResponse({"error": str(data["errors"])}, status_code=502)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    db.execute(
        "UPDATE library_entries SET progress = %s WHERE anime_id = %s AND user_id = %s",
        (progress, anime_id, user["id"]),
    )
    return JSONResponse({"ok": True, "progress": progress})


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

    return JSONResponse({
        "ok": True,
        "id": anime_id,
        "title": title_english or title_romaji,
        "status": status,
    })
