import html
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import bcrypt
import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from authlib.integrations.starlette_client import OAuth
from fastapi import BackgroundTasks, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

log = logging.getLogger("anime_tracker")

load_dotenv()

from app import db, config

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

_sync_lock = threading.Lock()
_sync_state: dict[int, dict] = {}  # user_id -> {"running": bool, "last_result": str|None}

_scheduler = BackgroundScheduler(timezone="UTC")


def _get_sync_state(user_id: int) -> dict:
    return _sync_state.setdefault(user_id, {"running": False, "last_result": None})


def _run_sync_task(user_id: int, script: str = _FULL_SYNC_SCRIPT) -> None:
    state = _get_sync_state(user_id)
    env = os.environ.copy()
    env["USER_ID"] = str(user_id)
    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=600, env=env,
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
    finally:
        state["running"] = False


def _tg_send(user_id: int, text: str) -> None:
    """Fire-and-forget Telegram message for one user. Silently drops if not configured."""
    token = config.get(user_id, "telegram_bot_token")
    chat_id = config.get(user_id, "telegram_chat_id")
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
        log.warning("Telegram send failed for user %s: %s", user_id, e)


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
            lines.append(f"▶ <b>{title}</b> — Ep {r['episode']} is now airing")
        _tg_send(user_id, "\n".join(lines))

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
        lines = ["<b>Anime this week:</b>"]
        for r in rows:
            title = r["title_english"] or r["title_romaji"]
            dt = r["airing_at"].strftime("%a %d %b %H:%M UTC") if r["airing_at"] else ""
            lines.append(f"• {title} — Ep {r['episode']} ({dt})")
        _tg_send(user_id, "\n".join(lines))


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
            _run_sync_task(user_id, _FULL_SYNC_SCRIPT)
        except Exception as e:
            log.error("Unhandled error syncing user %s: %s", user_id, e)
            state["running"] = False
            state["last_result"] = "error"
        result = state.get("last_result", "error")
        if result == "ok":
            _tg_send(user_id, "✅ Anime Tracker — daily sync completed successfully.")
        else:
            _tg_send(user_id, "❌ Anime Tracker — daily sync <b>failed</b>. Check container logs.")


def _scheduled_recommender() -> None:
    """Loop every user with sync credentials configured — same error isolation as sync."""
    for user in _users_with_sync_credentials():
        user_id = user["id"]
        log.info("Running scheduled recommender for user %s", user_id)
        env = os.environ.copy()
        env["USER_ID"] = str(user_id)
        try:
            subprocess.run([sys.executable, _RECOMMENDER_SCRIPT], env=env, timeout=600)
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
app.add_middleware(SessionMiddleware, secret_key=_SESSION_SECRET_KEY)

oauth = OAuth()  # clients registered dynamically per-request — see _ensure_oauth_registered

AUTH_PROVIDERS = {"google", "discord"}

_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_MINUTES = 15


@app.on_event("startup")
def startup() -> None:
    _apply_schedule()
    _scheduler.start()
    log.info("APScheduler started")


@app.on_event("shutdown")
def shutdown() -> None:
    _scheduler.shutdown(wait=False)
    log.info("APScheduler stopped")


# ── Auth ───────────────────────────────────────────────────────────────────────

def get_current_user(request: Request) -> dict | None:
    """Return the logged-in user's row, or None if no valid session."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.fetchone("SELECT * FROM users WHERE id = %s", (user_id,))


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


@app.get("/auth/login", response_class=HTMLResponse)
def auth_login_page(request: Request, error: str = ""):
    oauth_links = ""
    if oauth_configured("google"):
        oauth_links += '<p><a href="/auth/login/google">Log in with Google</a></p>'
    if oauth_configured("discord"):
        oauth_links += '<p><a href="/auth/login/discord">Log in with Discord</a></p>'
    error_html = f"<p style='color:red'>{html.escape(error)}</p>" if error else ""
    return HTMLResponse(f"""
        <h1>Log in</h1>
        {error_html}
        <form method="post" action="/auth/login">
            <input type="email" name="email" placeholder="email@example.com" required><br>
            <input type="password" name="password" placeholder="password" required><br>
            <button type="submit">Log in</button>
        </form>
        <p><a href="/auth/register">Need an account?</a></p>
        {oauth_links}
    """)


@app.post("/auth/login")
def auth_login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    user = db.fetchone(
        "SELECT * FROM users WHERE auth_provider = 'local' AND auth_provider_id = %s",
        (email,),
    )

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

    db.execute(
        "UPDATE users SET last_login_at = now(), failed_login_attempts = 0, locked_until = NULL WHERE id = %s",
        (user["id"],),
    )
    request.session["user_id"] = user["id"]
    return RedirectResponse(url="/", status_code=303)


@app.get("/auth/register", response_class=HTMLResponse)
def auth_register_page(request: Request, error: str = ""):
    error_html = f"<p style='color:red'>{html.escape(error)}</p>" if error else ""
    return HTMLResponse(f"""
        <h1>Create account</h1>
        {error_html}
        <form method="post" action="/auth/register">
            <input type="email" name="email" placeholder="email@example.com" required><br>
            <input type="password" name="password" placeholder="password (min 8 characters)" required minlength="8"><br>
            <button type="submit">Create account</button>
        </form>
        <p><a href="/auth/login">Already have an account?</a></p>
    """)


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

    request.session["user_id"] = user["id"]
    return RedirectResponse(url="/", status_code=303)


def _valid_reset_token(token: str):
    """Returns the password_resets row if the token is usable, else None."""
    row = db.fetchone(
        "SELECT * FROM password_resets WHERE token = %s AND used_at IS NULL AND expires_at > now()",
        (token,),
    )
    return row


@app.get("/auth/reset-password/{token}", response_class=HTMLResponse)
def auth_reset_password_page(token: str, error: str = ""):
    if not _valid_reset_token(token):
        return HTMLResponse(
            "<h1>Invalid or expired link</h1>"
            "<p>This password reset link is invalid, expired, or already used. "
            "Ask an admin to generate a new one.</p>",
            status_code=400,
        )
    error_html = f"<p style='color:red'>{html.escape(error)}</p>" if error else ""
    return HTMLResponse(f"""
        <h1>Set a new password</h1>
        {error_html}
        <form method="post" action="/auth/reset-password/{token}">
            <input type="password" name="password" placeholder="new password (min 8 characters)" required minlength="8"><br>
            <button type="submit">Set password</button>
        </form>
    """)


@app.post("/auth/reset-password/{token}")
def auth_reset_password_submit(token: str, password: str = Form(...)):
    reset = _valid_reset_token(token)
    if not reset:
        return HTMLResponse(
            "<h1>Invalid or expired link</h1>"
            "<p>This password reset link is invalid, expired, or already used. "
            "Ask an admin to generate a new one.</p>",
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

    request.session["user_id"] = user["id"]
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
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


def _require_admin(request: Request):
    """Returns a Response to send back if the caller isn't an admin, else None."""
    user = get_current_user(request)
    if not user:
        return HTMLResponse(
            "<h1>Not logged in</h1>"
            "<p><a href='/auth/login/google'>Log in with Google</a> or "
            "<a href='/auth/login/discord'>Log in with Discord</a></p>",
            status_code=401,
        )
    if not user["is_admin"]:
        return HTMLResponse("<h1>Forbidden</h1><p>Admin access required.</p>", status_code=403)
    return None


@app.get("/admin/invites", response_class=HTMLResponse)
def admin_invites(request: Request):
    denied = _require_admin(request)
    if denied:
        return denied

    invites = db.fetchall("SELECT * FROM invites ORDER BY created_at DESC")
    rows_html = "".join(
        f"<tr><td>{html.escape(i['email'])}</td><td>{'accepted' if i['accepted_at'] else 'pending'}</td>"
        f"<td>{i['created_at']}</td></tr>"
        for i in invites
    )

    now = datetime.now(timezone.utc)
    users = db.fetchall("SELECT * FROM users ORDER BY created_at DESC")
    users_rows_html = "".join(
        f"<tr><td>{html.escape(u['email'])}</td><td>{'yes' if u['is_admin'] else ''}</td>"
        f"<td>{u['created_at']}</td><td>{u['last_login_at'] or '—'}</td>"
        f"<td>{'🔒 locked' if u['locked_until'] and u['locked_until'] > now else ''}</td>"
        f"<td><form method='post' action='/admin/users/{u['id']}/reset-password' style='display:inline'>"
        f"<button type='submit'>Reset password</button></form></td></tr>"
        for u in users
    )

    def _provider_status(provider: str) -> str:
        client_id, client_secret = _oauth_config(provider)
        if client_id and client_secret:
            return f"configured (client id: {html.escape(client_id)})"
        return "not configured"

    return HTMLResponse(f"""
        <h1>Invites</h1>
        <form method="post" action="/admin/invites">
            <input type="email" name="email" placeholder="email@example.com" required>
            <button type="submit">Invite</button>
        </form>
        <table border="1" cellpadding="4">
            <tr><th>Email</th><th>Status</th><th>Created</th></tr>
            {rows_html}
        </table>

        <h1>Users</h1>
        <table border="1" cellpadding="4">
            <tr><th>Email</th><th>Admin</th><th>Created</th><th>Last login</th><th>Status</th><th></th></tr>
            {users_rows_html}
        </table>

        <h1>OAuth settings</h1>
        <p>Optional — local email/password login always works regardless of these.</p>

        <h2>Google — {_provider_status("google")}</h2>
        <form method="post" action="/admin/oauth-settings">
            <input type="hidden" name="provider" value="google">
            <input type="text" name="client_id" placeholder="Client ID"><br>
            <input type="text" name="client_secret" placeholder="Client Secret (leave blank to keep current)"><br>
            <button type="submit">Save</button>
        </form>

        <h2>Discord — {_provider_status("discord")}</h2>
        <form method="post" action="/admin/oauth-settings">
            <input type="hidden" name="provider" value="discord">
            <input type="text" name="client_id" placeholder="Client ID"><br>
            <input type="text" name="client_secret" placeholder="Client Secret (leave blank to keep current)"><br>
            <button type="submit">Save</button>
        </form>
    """)


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
    return RedirectResponse(url="/admin/invites", status_code=303)


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

    if client_id.strip():
        _instance_config_set(f"{provider}_client_id", client_id.strip())
    if client_secret.strip():
        _instance_config_set(f"{provider}_client_secret", client_secret.strip())

    return RedirectResponse(url="/admin/invites", status_code=303)


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
    reset_url = request.url_for("auth_reset_password_page", token=token)
    return HTMLResponse(f"""
        <h1>Password reset link for {html.escape(target['email'])}</h1>
        <p>Valid for 1 hour, single use. Copy this link now and send it to them —
        it won't be shown again:</p>
        <p><code>{reset_url}</code></p>
        <p><a href="/admin/invites">Back to admin</a></p>
    """)


def _require_user(request: Request):
    """Returns (user, None) if authenticated, or (None, redirect) to send back if not.
    For page routes — browser-friendly redirect to the login page."""
    user = get_current_user(request)
    if not user:
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


@app.get("/recommendations", response_class=HTMLResponse)
def recommendations(request: Request):
    user, denied = _require_user(request)
    if denied:
        return denied

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
        WHERE rs.dismissed = false AND rs.user_id = %s
        ORDER BY rs.score DESC
        LIMIT 100
        """,
        (user["id"],),
    )
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
        request,
        "recommendations.html",
        {"entries": entries},
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


@app.get("/anime/{anime_id}/notes", response_class=HTMLResponse)
def notes_form(request: Request, anime_id: int, back: str = "WATCHING"):
    user, denied = _require_user(request)
    if denied:
        return denied

    anime = db.fetchone(
        """
        SELECT a.id, a.title_english, a.title_romaji, a.cover_image_url, le.status,
               a.trailer_yt_id, a.relations
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
         "trailer": trailer, "related": related},
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

    return templates.TemplateResponse(
        request,
        "upcoming.html",
        {"entries": entries},
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
def settings_page(request: Request, link_error: str = ""):
    user, denied = _require_user(request)
    if denied:
        return denied

    current = config.get_all(user["id"])
    # Schedule fields are instance-wide (one cron trigger regardless of user count),
    # not per-user — merge them in from instance_config so the template can show them
    # alongside the genuinely per-user settings without the template needing to know
    # which table each field actually lives in.
    current["sync_daily_time"] = _instance_config_get("sync_daily_time") or "04:30"
    current["sync_recommender_day"] = _instance_config_get("sync_recommender_day") or "sun"
    current["sync_recommender_time"] = _instance_config_get("sync_recommender_time") or "05:00"

    row = db.fetchone(
        "SELECT MAX(synced_at) AS ts FROM library_entries WHERE user_id = %s", (user["id"],)
    )
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
        request,
        "settings.html",
        {
            "settings": current,
            "timezones": COMMON_TIMEZONES,
            "days_of_week": DAYS_OF_WEEK,
            "day_labels": DAY_LABELS,
            "last_synced": last_synced,
            "next_daily_sync": _next("daily_sync"),
            "next_recommender": _next("weekly_recommender"),
            "account": {
                "has_password": bool(user["password_hash"]),
                "google_linked": bool(user["google_id"]),
                "discord_linked": bool(user["discord_id"]),
            },
            "oauth_google_configured": oauth_configured("google"),
            "oauth_discord_configured": oauth_configured("discord"),
            "link_error": link_error,
        },
    )


DAYS_OF_WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
              "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}


@app.post("/settings")
def settings_save(
    request: Request,
    timezone: str = Form(...),
    anilist_username: str = Form(""),
    anilist_token: str = Form(""),
    cr_etp_rt: str = Form(""),
    netflix_id_cookie: str = Form(""),
    netflix_secure_id_cookie: str = Form(""),
    sync_daily_time: str = Form("04:30"),
    sync_recommender_day: str = Form("sun"),
    sync_recommender_time: str = Form("05:00"),
):
    user, denied = _require_user(request)
    if denied:
        return denied

    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        timezone = "Europe/London"

    config.set_value(user["id"], "timezone", timezone)
    config.set_value(user["id"], "anilist_username", anilist_username.strip())

    # Only overwrite token if a non-empty value was submitted (empty = leave unchanged)
    if anilist_token.strip():
        config.set_value(user["id"], "anilist_token", anilist_token.strip())
    if cr_etp_rt.strip():
        config.set_value(user["id"], "cr_etp_rt", cr_etp_rt.strip())
    if netflix_id_cookie.strip():
        config.set_value(user["id"], "netflix_id_cookie", netflix_id_cookie.strip())
    if netflix_secure_id_cookie.strip():
        config.set_value(user["id"], "netflix_secure_id_cookie", netflix_secure_id_cookie.strip())

    # Sync schedule is instance-wide (one cron trigger regardless of user count), so it
    # goes to instance_config rather than this user's own settings row — admin-only,
    # since a non-admin changing it would affect every other user's sync timing too.
    if user["is_admin"]:
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

    return RedirectResponse(url="/settings", status_code=303)


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


@app.get("/api/sync/log")
def sync_log(request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    rows = db.fetchall(
        "SELECT run_at, type, status, entries_updated, error_msg "
        "FROM sync_log WHERE user_id = %s ORDER BY run_at DESC LIMIT 20",
        (user["id"],),
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
def sync_status(request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    state = _get_sync_state(user["id"])
    row = db.fetchone(
        "SELECT MAX(synced_at) AS ts FROM library_entries WHERE user_id = %s", (user["id"],)
    )
    last_synced = row["ts"].isoformat() if row and row["ts"] else None
    return JSONResponse({
        "running": state["running"],
        "last_result": state["last_result"],
        "last_synced": last_synced,
    })


@app.get("/api/stats")
def stats_data(request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    status_rows = db.fetchall(
        "SELECT status, COUNT(*) AS cnt FROM library_entries WHERE user_id = %s "
        "GROUP BY status ORDER BY cnt DESC",
        (user["id"],),
    )
    score_rows = db.fetchall(
        "SELECT score::int AS score, COUNT(*) AS cnt FROM library_entries "
        "WHERE score IS NOT NULL AND user_id = %s GROUP BY score ORDER BY score",
        (user["id"],),
    )
    genre_rows = db.fetchall(
        """
        SELECT genre, COUNT(*) AS cnt
        FROM library_entries le
        JOIN anime a ON a.id = le.anime_id,
             jsonb_array_elements_text(a.genres) AS genre
        WHERE le.status = 'COMPLETED' AND le.user_id = %s
        GROUP BY genre ORDER BY cnt DESC LIMIT 12
        """,
        (user["id"],),
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
        WHERE le.user_id = %s
        """,
        (user["id"],),
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
    user, denied = _require_user(request)
    if denied:
        return denied
    return templates.TemplateResponse(request, "stats.html")


@app.get("/api/export")
def export_library(request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

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
        (user["id"],),
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


@app.post("/api/anime/{anime_id}/status")
async def set_status(anime_id: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    status = body.get("status", "").upper()
    if status not in VALID_STATUSES:
        return JSONResponse({"error": "invalid status"}, status_code=400)

    anilist_status = STATUS_TO_ANILIST.get(status, status)

    token = _get_anilist_token(user["id"])
    if not token:
        return JSONResponse({"error": "AniList token not configured"}, status_code=500)

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
            return JSONResponse({"error": str(data["errors"])}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    db.execute(
        """
        INSERT INTO library_entries (user_id, anime_id, status)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, anime_id) DO UPDATE SET status = EXCLUDED.status
        """,
        (user["id"], anime_id, status),
    )
    return JSONResponse({"ok": True, "status": status})


@app.post("/api/anime/{anime_id}/progress")
async def set_progress(anime_id: int, request: Request):
    user, denied = _require_user_api(request)
    if denied:
        return denied

    body = await request.json()
    progress = body.get("progress")
    if not isinstance(progress, int) or progress < 0:
        return JSONResponse({"error": "progress must be a non-negative integer"}, status_code=400)

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
        "title": media["title"].get("english") or media["title"].get("romaji"),
        "status": status,
    })
