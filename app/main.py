import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import httpx
from fastapi import BackgroundTasks, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

log = logging.getLogger("anime_tracker")

load_dotenv()

from app import db, config

ANILIST_TOKEN = os.getenv("ANILIST_TOKEN")

# ── Manual sync state ─────────────────────────────────────────────────────────
_SYNC_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "sync_anilist.py")
_sync_lock = threading.Lock()
_sync_state: dict = {"running": False, "last_result": None}

def _run_sync_task():
    try:
        result = subprocess.run(
            [sys.executable, _SYNC_SCRIPT],
            capture_output=True, text=True, timeout=300, env=os.environ.copy(),
        )
        if result.returncode == 0:
            _sync_state["last_result"] = "ok"
        else:
            _sync_state["last_result"] = "error"
            log.error("Manual sync stderr: %s", result.stderr[-800:])
    except Exception as e:
        _sync_state["last_result"] = "error"
        log.error("Manual sync exception: %s", e)
    finally:
        _sync_state["running"] = False
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
    return templates.TemplateResponse(
        "recommendations.html",
        {"request": request, "entries": rows},
    )


@app.post("/recommendations/{anime_id}/dismiss")
def dismiss(anime_id: int):
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
    back: str = Form("WATCHING"),
):
    drop_reason_val = drop_reason.strip() or None
    notes_val = notes.strip() or None
    tags = [t.strip() for t in personal_tags.split(",") if t.strip()]
    try:
        priority = int(watch_next_priority.strip()) if watch_next_priority.strip() else None
    except ValueError:
        priority = None

    db.execute(
        """
        INSERT INTO personal_notes (anime_id, drop_reason, personal_tags, notes, watch_next_priority)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (anime_id) DO UPDATE SET
            drop_reason = EXCLUDED.drop_reason,
            personal_tags = EXCLUDED.personal_tags,
            notes = EXCLUDED.notes,
            watch_next_priority = EXCLUDED.watch_next_priority,
            updated_at = now()
        """,
        (anime_id, drop_reason_val, json.dumps(tags), notes_val, priority),
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

    db.execute(
        """
        INSERT INTO personal_notes (anime_id, drop_reason, personal_tags, notes, watch_next_priority)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (anime_id) DO UPDATE SET
            drop_reason = EXCLUDED.drop_reason,
            personal_tags = EXCLUDED.personal_tags,
            notes = EXCLUDED.notes,
            watch_next_priority = EXCLUDED.watch_next_priority,
            updated_at = now()
        """,
        (anime_id, drop_reason_val, json.dumps(tags), notes_val, priority),
    )
    return JSONResponse({"ok": True})


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
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": current,
            "timezones": COMMON_TIMEZONES,
            "last_synced": last_synced,
        },
    )


@app.post("/settings")
def settings_save(timezone: str = Form(...)):
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        timezone = "Europe/London"
    config.set_value("timezone", timezone)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/api/sync")
async def trigger_sync(background_tasks: BackgroundTasks):
    with _sync_lock:
        if _sync_state["running"]:
            return JSONResponse({"status": "already_running"})
        _sync_state["running"] = True
        _sync_state["last_result"] = None
    background_tasks.add_task(_run_sync_task)
    return JSONResponse({"status": "started"})


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
            COUNT(*) FILTER (WHERE status = 'COMPLETED') AS completed,
            COUNT(*) FILTER (WHERE status = 'WATCHING')  AS watching,
            COALESCE(SUM(progress), 0)                   AS total_episodes
        FROM library_entries
        """
    )
    return JSONResponse({
        "status": [{"label": r["status"].title(), "value": r["cnt"]} for r in status_rows],
        "scores": [{"score": r["score"], "count": r["cnt"]} for r in score_rows],
        "genres": [{"genre": r["genre"], "count": r["cnt"]} for r in genre_rows],
        "by_year": [{"year": r["year"], "count": r["cnt"]} for r in year_rows],
        "totals": {
            "completed": totals["completed"],
            "watching": totals["watching"],
            "total_episodes": int(totals["total_episodes"]),
        },
    })


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    return templates.TemplateResponse("stats.html", {"request": request})


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

    if not ANILIST_TOKEN:
        return JSONResponse({"error": "ANILIST_TOKEN not configured"}, status_code=500)

    try:
        resp = httpx.post(
            ANILIST_API,
            json={"query": SAVE_SCORE_MUTATION, "variables": {"mediaId": anime_id, "score": anilist_score}},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {ANILIST_TOKEN}"},
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

    if not ANILIST_TOKEN:
        return JSONResponse({"error": "ANILIST_TOKEN not configured"}, status_code=500)

    try:
        resp = httpx.post(
            ANILIST_API,
            json={"query": SAVE_STATUS_MUTATION, "variables": {"mediaId": anime_id, "status": anilist_status}},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {ANILIST_TOKEN}"},
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

    if not ANILIST_TOKEN:
        return JSONResponse({"error": "ANILIST_TOKEN not configured"}, status_code=500)

    try:
        resp = httpx.post(
            ANILIST_API,
            json={"query": SAVE_PROGRESS_MUTATION, "variables": {"mediaId": anime_id, "progress": progress}},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {ANILIST_TOKEN}"},
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
