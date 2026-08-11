import json
import logging
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

log = logging.getLogger("anime_tracker")

load_dotenv()

from app import db, config

def _get_anilist_token() -> str:
    """Return AniList token from settings DB, falling back to env var."""
    return config.get("anilist_token") or os.getenv("ANILIST_TOKEN", "")

# ── Sync orchestration ────────────────────────────────────────────────────────
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
_FULL_SYNC_SCRIPT = os.path.join(_SCRIPTS_DIR, "run_full_sync.py")
_RECOMMENDER_SCRIPT = os.path.join(_SCRIPTS_DIR, "run_recommender.py")

_sync_lock = threading.Lock()
_sync_state: dict = {"running": False, "last_result": None}

_scheduler = BackgroundScheduler(timezone="UTC")


def _run_sync_task(script: str = _FULL_SYNC_SCRIPT) -> None:
    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=600, env=os.environ.copy(),
        )
        if result.returncode == 0:
            _sync_state["last_result"] = "ok"
            log.info("Sync completed: %s", script)
        else:
            _sync_state["last_result"] = "error"
            log.error("Sync failed (%s) stderr: %s", script, result.stderr[-800:])
    except Exception as e:
        _sync_state["last_result"] = "error"
        log.error("Sync exception (%s): %s", script, e)
    finally:
        _sync_state["running"] = False


def _tg_send(text: str) -> None:
    """Fire-and-forget Telegram message. Silently drops if credentials not set."""
    token = config.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = config.get("telegram_chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        import httpx as _httpx
        _httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


def _check_airing_episodes() -> None:
    """Hourly job: notify for any unwatched episodes that started airing."""
    rows = db.fetchall(
        """
        SELECT a.title_english, a.title_romaji, asc_.episode, asc_.airing_at
        FROM airing_schedule_cache asc_
        JOIN anime a ON a.id = asc_.anime_id
        JOIN library_entries le ON le.anime_id = asc_.anime_id
        WHERE asc_.notified = false
          AND asc_.airing_at <= now()
          AND le.status IN ('WATCHING', 'PLANNING')
        ORDER BY asc_.airing_at
        """
    )
    if not rows:
        return
    lines = []
    for r in rows:
        title = r["title_english"] or r["title_romaji"]
        lines.append(f"▶ <b>{title}</b> — Ep {r['episode']} is now airing")
    _tg_send("\n".join(lines))

    db.execute(
        """
        UPDATE airing_schedule_cache SET notified = true
        WHERE notified = false AND airing_at <= now()
          AND anime_id IN (
              SELECT anime_id FROM library_entries WHERE status IN ('WATCHING', 'PLANNING')
          )
        """
    )


def _weekly_airing_digest() -> None:
    """Monday morning digest: upcoming episodes in the next 7 days."""
    rows = db.fetchall(
        """
        SELECT a.title_english, a.title_romaji, asc_.episode, asc_.airing_at
        FROM airing_schedule_cache asc_
        JOIN anime a ON a.id = asc_.anime_id
        JOIN library_entries le ON le.anime_id = asc_.anime_id
        WHERE asc_.airing_at BETWEEN now() AND now() + INTERVAL '7 days'
          AND le.status IN ('WATCHING', 'PLANNING')
        ORDER BY asc_.airing_at
        """
    )
    if not rows:
        return
    lines = ["<b>Anime this week:</b>"]
    for r in rows:
        title = r["title_english"] or r["title_romaji"]
        dt = r["airing_at"].strftime("%a %d %b %H:%M UTC") if r["airing_at"] else ""
        lines.append(f"• {title} — Ep {r['episode']} ({dt})")
    _tg_send("\n".join(lines))


def _scheduled_full_sync() -> None:
    with _sync_lock:
        if _sync_state["running"]:
            log.warning("Scheduled sync skipped — already running")
            return
        _sync_state["running"] = True
        _sync_state["last_result"] = None
    _run_sync_task(_FULL_SYNC_SCRIPT)
    result = _sync_state.get("last_result", "error")
    if result == "ok":
        _tg_send("✅ Anime Tracker — daily sync completed successfully.")
    else:
        _tg_send("❌ Anime Tracker — daily sync <b>failed</b>. Check container logs.")


def _scheduled_recommender() -> None:
    log.info("Running scheduled recommender")
    subprocess.run([sys.executable, _RECOMMENDER_SCRIPT], env=os.environ.copy(), timeout=600)


def _apply_schedule() -> None:
    """Read schedule from settings DB and (re)configure APScheduler jobs."""
    daily_time = config.get("sync_daily_time") or "04:30"
    rec_day    = config.get("sync_recommender_day") or "sunday"
    rec_time   = config.get("sync_recommender_time") or "05:00"

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
        _check_airing_episodes,
        CronTrigger(minute=0, timezone="UTC"),
        id="airing_check", replace_existing=True,
    )
    _scheduler.add_job(
        _weekly_airing_digest,
        CronTrigger(day_of_week="mon", hour=7, minute=0, timezone="UTC"),
        id="weekly_digest", replace_existing=True,
    )
ANILIST_API = "https://graphql.anilist.co"

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

SAVE_PROGRESS_MUTATION = """
mutation ($mediaId: Int!, $progress: Int!) {
  SaveMediaListEntry(mediaId: $mediaId, progress: $progress) {
    id progress
  }
}
"""

VALID_STATUSES = {"WATCHING", "COMPLETED", "DROPPED", "PLANNING", "PAUSED", "REPEATING"}
STATUS_TO_ANILIST = {"WATCHING": "CURRENT"}

GRAFANA_PUBLIC_URL = os.getenv("GRAFANA_PUBLIC_URL", "")

STREAMING_SITES = {
    "Crunchyroll", "Netflix", "Hulu", "Amazon Prime Video", "HIDIVE",
    "Disney Plus", "Bilibili TV", "Bilibili", "iQ", "WeTV", "Tubi TV",
    "Adult Swim", "Hoopla", "Max", "Tencent Video", "Bandai Channel",
    "Niconico Video", "Funimation", "VRV",
}
GRAFANA_EMBED_URL = os.getenv("GRAFANA_EMBED_URL", GRAFANA_PUBLIC_URL)
STATS_DASHBOARD_URL = (
    f"{GRAFANA_EMBED_URL}/d/anime-tracker-stats/anime-tracker-stats"
    "?kiosk=tv&theme=dark&orgId=1&from=now-10y&to=now"
)

app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def startup() -> None:
    _apply_schedule()
    _scheduler.start()
    log.info("APScheduler started")


@app.on_event("shutdown")
def shutdown() -> None:
    _scheduler.shutdown(wait=False)
    log.info("APScheduler stopped")


@app.get("/recommendations", response_class=HTMLResponse)
def recommendations(request: Request):
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
            rs.score       AS rec_score,
            rs.reason
        FROM recommendation_scores rs
        JOIN anime a ON a.id = rs.anime_id
        WHERE rs.dismissed = false
        ORDER BY rs.score DESC
        LIMIT 100
        """
    )
    # Build genre → completed_anime index for "similar to" lookup
    completed = db.fetchall(
        """
        SELECT a.id, a.title_english, a.title_romaji, a.genres
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id
        WHERE le.status = 'COMPLETED' AND a.genres IS NOT NULL
        """
    )
    comp_genres: list[tuple[int, str, list]] = [
        (r["id"], r["title_english"] or r["title_romaji"], r["genres"] or [])
        for r in completed
    ]

    entries = []
    for row in rows:
        rec_genres = set(row["genres"] or [])
        best_title, best_overlap = None, 0
        for _, title, cg in comp_genres:
            overlap = len(rec_genres & set(cg))
            if overlap > best_overlap:
                best_overlap, best_title = overlap, title
        entry = dict(row)
        entry["similar_to"] = best_title if best_overlap >= 2 else None
        entries.append(entry)

    return templates.TemplateResponse(
        "recommendations.html",
        {"request": request, "entries": entries},
    )


@app.post("/recommendations/{anime_id}/dismiss")
async def dismiss(anime_id: int, request: Request):
    reason = None
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        reason = body.get("reason") or None
        db.execute(
            "UPDATE recommendation_scores SET dismissed = true, dismiss_reason = %s WHERE anime_id = %s",
            (reason, anime_id),
        )
        return JSONResponse({"ok": True})
    db.execute(
        "UPDATE recommendation_scores SET dismissed = true WHERE anime_id = %s",
        (anime_id,),
    )
    return RedirectResponse(url="/recommendations", status_code=303)


@app.get("/anime/{anime_id}/notes", response_class=HTMLResponse)
def notes_form(request: Request, anime_id: int, back: str = "WATCHING"):
    anime = db.fetchone(
        """
        SELECT a.id, a.title_english, a.title_romaji, a.cover_image_url, le.status
        FROM anime a
        LEFT JOIN library_entries le ON le.anime_id = a.id
        WHERE a.id = %s
        """,
        (anime_id,),
    )
    notes = db.fetchone(
        "SELECT * FROM personal_notes WHERE anime_id = %s", (anime_id,)
    )
    return templates.TemplateResponse(
        "notes.html",
        {"request": request, "anime": anime, "notes": notes, "back": back},
    )


@app.post("/anime/{anime_id}/notes")
def save_notes(
    anime_id: int,
    drop_reason: str = Form(""),
    notes: str = Form(""),
    personal_tags: str = Form(""),
    watch_next_priority: str = Form(""),
    anilist_id_override: str = Form(""),
    back: str = Form("WATCHING"),
):
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
        INSERT INTO personal_notes (anime_id, drop_reason, personal_tags, notes, watch_next_priority, anilist_id_override)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (anime_id) DO UPDATE SET
            drop_reason = EXCLUDED.drop_reason,
            personal_tags = EXCLUDED.personal_tags,
            notes = EXCLUDED.notes,
            watch_next_priority = EXCLUDED.watch_next_priority,
            anilist_id_override = EXCLUDED.anilist_id_override,
            updated_at = now()
        """,
        (anime_id, drop_reason_val, json.dumps(tags), notes_val, priority, al_override),
    )
    return RedirectResponse(url=f"/?status={back}", status_code=303)


@app.post("/api/anime/{anime_id}/notes")
async def save_notes_api(anime_id: int, request: Request):
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
        INSERT INTO personal_notes (anime_id, drop_reason, personal_tags, notes, watch_next_priority, anilist_id_override)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (anime_id) DO UPDATE SET
            drop_reason = EXCLUDED.drop_reason,
            personal_tags = EXCLUDED.personal_tags,
            notes = EXCLUDED.notes,
            watch_next_priority = EXCLUDED.watch_next_priority,
            anilist_id_override = EXCLUDED.anilist_id_override,
            updated_at = now()
        """,
        (anime_id, drop_reason_val, json.dumps(tags), notes_val, priority, al_override),
    )
    return JSONResponse({"ok": True})


_EXTRA_QUERY = """
query ($id: Int!) {
  Media(id: $id, type: ANIME) {
    trailer { id site }
    relations {
      edges {
        relationType
        node {
          id
          title { romaji english }
          coverImage { large }
          format
        }
      }
    }
  }
}
"""

_RELATION_ORDER = ["PREQUEL", "SEQUEL", "PARENT", "SIDE_STORY", "SPIN_OFF",
                   "ALTERNATIVE", "COMPILATION", "CONTAINS", "SUMMARY", "OTHER"]


def _yt_trailer_from_url(url: str) -> dict | None:
    m = re.search(r'(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})', url)
    if not m:
        return None
    vid = m.group(1)
    return {
        "url": f"https://www.youtube.com/watch?v={vid}",
        "thumbnail": f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
    }


@app.get("/api/anime/{anime_id}/extra")
def anime_extra(anime_id: int):
    # Trailer: check DB external_links for YouTube first (already synced)
    row = db.fetchone("SELECT external_links FROM anime WHERE id = %s", (anime_id,))
    trailer = None
    for link in (row["external_links"] if row else []) or []:
        if link.get("site", "").lower() == "youtube":
            trailer = _yt_trailer_from_url(link["url"])
            if trailer:
                break

    # AniList call for relations (and trailer fallback if DB had nothing)
    data = {}
    try:
        resp = httpx.post(
            ANILIST_API,
            json={"query": _EXTRA_QUERY, "variables": {"id": anime_id}},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {}).get("Media") or {}
    except Exception as e:
        log.warning("AniList extra fetch failed for %s: %s", anime_id, e)

    # Fall back to AniList trailer field if external_links had nothing
    if not trailer:
        t = data.get("trailer") or {}
        if t.get("id") and t.get("site", "").lower() == "youtube":
            trailer = {
                "url": f"https://www.youtube.com/watch?v={t['id']}",
                "thumbnail": f"https://img.youtube.com/vi/{t['id']}/mqdefault.jpg",
            }

    relations = []
    for edge in (data.get("relations") or {}).get("edges", []):
        node = edge.get("node") or {}
        rel_type = edge.get("relationType", "OTHER")
        title = (node.get("title") or {})
        relations.append({
            "id": node["id"],
            "title": title.get("english") or title.get("romaji", ""),
            "cover": (node.get("coverImage") or {}).get("large"),
            "format": node.get("format"),
            "relation_type": rel_type,
        })
    relations.sort(key=lambda r: _RELATION_ORDER.index(r["relation_type"])
                   if r["relation_type"] in _RELATION_ORDER else 99)

    return JSONResponse({"trailer": trailer, "related": relations})


@app.get("/upcoming", response_class=HTMLResponse)
def upcoming(request: Request):
    tz_name = config.get("timezone")
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
        WHERE asc2.airing_at > now()
        ORDER BY asc2.airing_at
        """
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

    return templates.TemplateResponse(
        "upcoming.html",
        {"request": request, "entries": entries},
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
    queue_statuses = ["ALL", "PLANNING", "PAUSED"]
    active_status = status.upper() if status and status.upper() in queue_statuses else "ALL"

    status_filter = (
        "le.status IN ('PLANNING', 'PAUSED')"
        if active_status == "ALL"
        else "le.status = %s"
    )
    params = () if active_status == "ALL" else (active_status,)

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
        LEFT JOIN personal_notes pn ON pn.anime_id = a.id
        LEFT JOIN recommendation_scores rs
               ON rs.anime_id = a.id AND rs.dismissed = false
        WHERE {status_filter}
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
        "queue.html",
        {
            "request": request,
            "entries": entries,
            "queue_statuses": queue_statuses,
            "active_status": active_status,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    current = config.get_all()
    row = db.fetchone("SELECT MAX(synced_at) AS ts FROM library_entries")
    last_synced = row["ts"].isoformat() if row and row["ts"] else None

    # Next run times from scheduler
    def _next(job_id: str) -> str | None:
        try:
            job = _scheduler.get_job(job_id)
            if job and job.next_run_time:
                return job.next_run_time.isoformat()
        except Exception:
            pass
        return None

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": current,
            "timezones": COMMON_TIMEZONES,
            "days_of_week": DAYS_OF_WEEK,
            "day_labels": DAY_LABELS,
            "last_synced": last_synced,
            "next_daily_sync": _next("daily_sync"),
            "next_recommender": _next("weekly_recommender"),
        },
    )


DAYS_OF_WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
              "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}


@app.post("/settings")
def settings_save(
    timezone: str = Form(...),
    anilist_username: str = Form(""),
    anilist_token: str = Form(""),
    cr_etp_rt: str = Form(""),
    sync_daily_time: str = Form("04:30"),
    sync_recommender_day: str = Form("sunday"),
    sync_recommender_time: str = Form("05:00"),
):
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        timezone = "Europe/London"

    config.set_value("timezone", timezone)
    config.set_value("anilist_username", anilist_username.strip())

    # Only overwrite token if a non-empty value was submitted (empty = leave unchanged)
    if anilist_token.strip():
        config.set_value("anilist_token", anilist_token.strip())
    if cr_etp_rt.strip():
        config.set_value("cr_etp_rt", cr_etp_rt.strip())

    # Validate and save schedule
    try:
        h, m = sync_daily_time.split(":")
        assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except Exception:
        sync_daily_time = "04:30"
    config.set_value("sync_daily_time", sync_daily_time)

    if sync_recommender_day not in DAYS_OF_WEEK:
        sync_recommender_day = "sun"
    config.set_value("sync_recommender_day", sync_recommender_day)

    try:
        h, m = sync_recommender_time.split(":")
        assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except Exception:
        sync_recommender_time = "05:00"
    config.set_value("sync_recommender_time", sync_recommender_time)

    # Apply new schedule immediately — no restart needed
    _apply_schedule()

    return RedirectResponse(url="/settings", status_code=303)


@app.post("/api/sync")
async def trigger_sync(background_tasks: BackgroundTasks):
    with _sync_lock:
        if _sync_state["running"]:
            return JSONResponse({"status": "already_running"})
        _sync_state["running"] = True
        _sync_state["last_result"] = None
    background_tasks.add_task(_run_sync_task, _FULL_SYNC_SCRIPT)
    return JSONResponse({"status": "started"})


@app.get("/api/sync/log")
def sync_log():
    rows = db.fetchall(
        "SELECT run_at, type, status, entries_updated, error_msg "
        "FROM sync_log ORDER BY run_at DESC LIMIT 20"
    )
    return JSONResponse([
        {
            "run_at": r["run_at"].isoformat(),
            "type": r["type"],
            "status": r["status"],
            "entries_updated": r["entries_updated"],
            "error_msg": r["error_msg"],
        }
        for r in rows
    ])


@app.get("/api/sync/status")
def sync_status():
    row = db.fetchone("SELECT MAX(synced_at) AS ts FROM library_entries")
    last_synced = row["ts"].isoformat() if row and row["ts"] else None
    return JSONResponse({
        "running": _sync_state["running"],
        "last_result": _sync_state["last_result"],
        "last_synced": last_synced,
    })


@app.get("/api/stats")
def stats_data():
    status_rows = db.fetchall(
        "SELECT status, COUNT(*) AS cnt FROM library_entries GROUP BY status ORDER BY cnt DESC"
    )
    score_rows = db.fetchall(
        "SELECT score::int AS score, COUNT(*) AS cnt FROM library_entries "
        "WHERE score IS NOT NULL GROUP BY score ORDER BY score"
    )
    genre_rows = db.fetchall(
        """
        SELECT genre, COUNT(*) AS cnt
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id,
             jsonb_array_elements_text(a.genres) AS genre
        WHERE le.status = 'COMPLETED'
        GROUP BY genre ORDER BY cnt DESC LIMIT 12
        """
    )
    year_rows = db.fetchall(
        """
        SELECT a.season_year AS year, COUNT(*) AS cnt
        FROM library_entries le JOIN anime a ON a.id = le.anime_id
        WHERE le.status = 'COMPLETED' AND a.season_year IS NOT NULL AND a.season_year >= 2010
        GROUP BY a.season_year ORDER BY a.season_year
        """
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
        """
    )
    completed = int(totals["completed"])
    dropped = int(totals["dropped"])
    completion_rate = round(completed / (completed + dropped) * 100) if (completed + dropped) > 0 else None
    watch_minutes = int(totals["watch_minutes"])
    watch_hours = watch_minutes // 60
    watch_days = round(watch_minutes / 1440, 1)
    return JSONResponse({
        "status": [{"label": r["status"].title(), "value": r["cnt"]} for r in status_rows],
        "scores": [{"score": r["score"], "count": r["cnt"]} for r in score_rows],
        "genres": [{"genre": r["genre"], "count": r["cnt"]} for r in genre_rows],
        "by_year": [{"year": r["year"], "count": r["cnt"]} for r in year_rows],
        "totals": {
            "completed": completed,
            "watching": int(totals["watching"]),
            "total_episodes": int(totals["total_episodes"]),
            "watch_hours": watch_hours,
            "watch_days": watch_days,
            "completion_rate": completion_rate,
            "mean_score": float(totals["mean_score"]) if totals["mean_score"] else None,
        },
    })


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    return templates.TemplateResponse("stats.html", {"request": request})


@app.get("/api/export")
def export_library():
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
        LEFT JOIN personal_notes pn ON pn.anime_id = a.id
        ORDER BY le.status, a.title_romaji
        """
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
    from fastapi.responses import Response as _Response
    import json as _json
    return _Response(
        content=_json.dumps(export, default=str, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=anime_library_export.json"},
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
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
                a.title_english ILIKE %s
                OR a.title_romaji ILIKE %s
                OR a.title_native ILIKE %s
            ORDER BY le.status, a.title_romaji
            """,
            (pattern, pattern, pattern),
        )
        for row in rows:
            entry = dict(row)
            entry["streaming_links"] = [
                lnk for lnk in (row["external_links"] or [])
                if lnk.get("site") in STREAMING_SITES
            ]
            entries.append(entry)
    return templates.TemplateResponse(
        "search.html",
        {"request": request, "q": q, "entries": entries},
    )


@app.get("/", response_class=HTMLResponse)
def library(request: Request, response: Response, status: str = None):
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
        LEFT JOIN personal_notes pn ON pn.anime_id = a.id
        LEFT JOIN LATERAL (
            SELECT episode, airing_at
            FROM airing_schedule_cache
            WHERE anime_id = a.id AND airing_at > now()
            ORDER BY airing_at
            LIMIT 1
        ) next_ep ON true
        WHERE le.status = %s
        ORDER BY le.score DESC NULLS LAST, a.title_romaji
        """,
        (active_status,),
    )

    stale_threshold = datetime.now(timezone.utc) - timedelta(days=60)
    tz = ZoneInfo(config.get("timezone") or "Europe/London")

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
        "library.html",
        {
            "request": request,
            "entries": entries,
            "statuses": statuses,
            "active_status": active_status,
        },
    )


@app.post("/api/anime/{anime_id}/rating")
async def set_rating(anime_id: int, request: Request):
    body = await request.json()
    stars = int(body.get("score", 0))
    if stars < 0 or stars > 5:
        return JSONResponse({"error": "score must be 0–5"}, status_code=400)

    # AniList account uses POINT_5 format — send 0–5 directly.
    # Reading back via sync uses score(format: POINT_100), which AniList converts correctly.
    anilist_score = float(stars)

    if not _get_anilist_token():
        return JSONResponse({"error": "AniList token not configured"}, status_code=500)

    try:
        resp = httpx.post(
            ANILIST_API,
            json={"query": SAVE_SCORE_MUTATION, "variables": {"mediaId": anime_id, "score": anilist_score}},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {_get_anilist_token()}"},
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
    db.execute("UPDATE library_entries SET score = %s WHERE anime_id = %s", (local_score, anime_id))

    return JSONResponse({"ok": True, "score": stars})


@app.post("/api/anime/{anime_id}/status")
async def set_status(anime_id: int, request: Request):
    body = await request.json()
    status = body.get("status", "").upper()
    if status not in VALID_STATUSES:
        return JSONResponse({"error": "invalid status"}, status_code=400)

    anilist_status = STATUS_TO_ANILIST.get(status, status)

    if not _get_anilist_token():
        return JSONResponse({"error": "AniList token not configured"}, status_code=500)

    try:
        resp = httpx.post(
            ANILIST_API,
            json={"query": SAVE_STATUS_MUTATION, "variables": {"mediaId": anime_id, "status": anilist_status}},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {_get_anilist_token()}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            return JSONResponse({"error": str(data["errors"])}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    db.execute(
        """
        INSERT INTO library_entries (anime_id, status)
        VALUES (%s, %s)
        ON CONFLICT (anime_id) DO UPDATE SET status = EXCLUDED.status
        """,
        (anime_id, status),
    )
    return JSONResponse({"ok": True, "status": status})


@app.post("/api/anime/{anime_id}/progress")
async def set_progress(anime_id: int, request: Request):
    body = await request.json()
    progress = body.get("progress")
    if not isinstance(progress, int) or progress < 0:
        return JSONResponse({"error": "progress must be a non-negative integer"}, status_code=400)

    if not _get_anilist_token():
        return JSONResponse({"error": "AniList token not configured"}, status_code=500)

    try:
        resp = httpx.post(
            ANILIST_API,
            json={"query": SAVE_PROGRESS_MUTATION, "variables": {"mediaId": anime_id, "progress": progress}},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {_get_anilist_token()}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            return JSONResponse({"error": str(data["errors"])}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    db.execute("UPDATE library_entries SET progress = %s WHERE anime_id = %s", (progress, anime_id))
    return JSONResponse({"ok": True, "progress": progress})


@app.post("/api/queue/reorder")
async def reorder_queue(request: Request):
    """Accept [{anime_id, priority}] and bulk-update personal_notes.watch_next_priority."""
    body = await request.json()
    items = body if isinstance(body, list) else []
    for item in items:
        anime_id = int(item["anime_id"])
        priority = item.get("priority")
        db.execute(
            """
            INSERT INTO personal_notes (anime_id, watch_next_priority)
            VALUES (%s, %s)
            ON CONFLICT (anime_id) DO UPDATE SET watch_next_priority = EXCLUDED.watch_next_priority
            """,
            (anime_id, priority),
        )
    return JSONResponse({"ok": True})
