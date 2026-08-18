#!/usr/bin/env python3
"""
Shared AniList query/matching/update helpers, used by every sync_*.py script
(Crunchyroll, Netflix, Prime Video). Each sync script is invoked as its own
subprocess (see run_full_sync.py) with USER_ID/ANILIST_TOKEN/ANILIST_USERNAME/
DATABASE_URL in its env, so this module loads its own env rather than relying
on the importing script having done it first.
"""

import os
import time

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

ANILIST_TOKEN = os.environ["ANILIST_TOKEN"]
ANILIST_USERNAME = os.environ["ANILIST_USERNAME"]
ANILIST_API = "https://graphql.anilist.co"
DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])


MAX_RATE_LIMIT_RETRIES = 5


def gql(query: str, variables: dict | None = None, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # AniList's rate limit (30 req/min at time of writing) is real to hit in
    # practice — confirmed live during #48's testing, not just a theoretical edge
    # case: a single sync with several search-fallback matches plus an update call
    # can trip it, especially on a large first-time pull. Retry with the delay
    # AniList itself specifies (Retry-After) rather than failing the whole sync.
    for attempt in range(MAX_RATE_LIMIT_RETRIES):
        resp = httpx.post(
            ANILIST_API,
            json={"query": query, "variables": variables or {}},
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 429 and attempt < MAX_RATE_LIMIT_RETRIES - 1:
            wait = float(resp.headers.get("retry-after", 5))
            time.sleep(wait)
            continue
        break

    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"AniList error: {data['errors']}")
    return data["data"]


ALL_LISTS_QUERY = """
query ($userName: String) {
  MediaListCollection(userName: $userName, type: ANIME) {
    lists {
      entries {
        mediaId
        progress
        status
        repeat
        media {
          id
          episodes
          format
          title { romaji english }
        }
      }
    }
  }
}
"""

SEARCH_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    format
    title { romaji english }
  }
}
"""


def fetch_user_list() -> tuple[dict[int, dict], dict[str, int]]:
    """Fetch all the user's AniList entries in one call.

    Returns (entries_by_id, title_index) where title_index maps
    lowercased romaji/english titles → mediaId for fast title matching.
    """
    data = gql(ALL_LISTS_QUERY, {"userName": ANILIST_USERNAME})
    entries: dict[int, dict] = {}
    title_index: dict[str, int] = {}

    for lst in data["MediaListCollection"]["lists"]:
        for entry in lst["entries"]:
            mid = entry["mediaId"]
            media = entry.get("media") or {}
            title_obj = media.get("title") or {}
            romaji = (title_obj.get("romaji") or "").strip()
            english = (title_obj.get("english") or "").strip()

            entries[mid] = {
                "status": entry["status"],
                "progress": entry["progress"] or 0,
                "repeat": entry["repeat"] or 0,
                "total_episodes": media.get("episodes"),
                "format": media.get("format"),
                "title": english or romaji or "",
            }

            for t in (romaji, english):
                if t:
                    title_index[t.lower()] = mid

    return entries, title_index


def load_user_list_from_db() -> tuple[dict[int, dict], dict[str, int]]:
    """Local-mirror equivalent of fetch_user_list() (issue #99) — reads the AniList
    library from Postgres's library_entries/anime tables instead of making a live
    AniList API call. Crunchyroll and Netflix sync were each independently calling
    fetch_user_list() once per pipeline run (2x redundant today, 3x once Prime Video
    exists); run_full_sync.py now runs the anilist_postgres step first specifically so
    this mirror is fresh before crunchyroll/netflix matching needs it.

    Same return shape as fetch_user_list() — callers can swap between the two with a
    one-line change, and find_anilist_id()/is_plausible_match() are unchanged either
    way, since only the *source* of this data changed, not its shape."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    library_entries.anime_id AS media_id,
                    library_entries.status,
                    library_entries.progress,
                    library_entries.repeat_count,
                    anime.episodes AS total_episodes,
                    anime.format,
                    anime.title_romaji,
                    anime.title_english
                FROM library_entries
                JOIN anime ON anime.id = library_entries.anime_id
                WHERE library_entries.user_id = %s
            """, (USER_ID,))
            rows = cur.fetchall()
    finally:
        conn.close()

    entries: dict[int, dict] = {}
    title_index: dict[str, int] = {}

    for row in rows:
        mid = row["media_id"]
        romaji = (row["title_romaji"] or "").strip()
        english = (row["title_english"] or "").strip()

        entries[mid] = {
            "status": row["status"],
            "progress": row["progress"] or 0,
            "repeat": row["repeat_count"] or 0,
            "total_episodes": row["total_episodes"],
            "format": row["format"],
            "title": english or romaji or "",
        }

        for t in (romaji, english):
            if t:
                title_index[t.lower()] = mid

    return entries, title_index


# In-process only by default — issue #115: callers with a DB connection (every
# sync_*.py script) seed this from anilist_title_search_cache via seed_search_cache()
# before matching, and persist new entries back via search_cache_snapshot() after, so
# repeat searches for the same permanently-non-matching titles don't cost anything on a
# later sync/retry. Kept as a plain in-process dict here (not DB-backed directly) so
# this module stays a pure AniList-API helper with no psycopg2 dependency of its own.
_search_cache: dict[str, int | None] = {}


def seed_search_cache(entries: dict[str, int | None]) -> None:
    """Pre-populate the in-process search cache from persisted data (issue #115)."""
    _search_cache.update(entries)


def search_cache_snapshot() -> dict[str, int | None]:
    """Current contents of the in-process search cache — includes both seeded and
    newly-resolved entries, so a caller can upsert the whole thing back to
    anilist_title_search_cache unconditionally without tracking what's "new" (issue
    #115). Safe to call at any point, including after an interrupted/timed-out run,
    since it just reflects whatever's been resolved so far."""
    return dict(_search_cache)


def find_anilist_id(title: str, title_index: dict[str, int]) -> int | None:
    """Return AniList media ID for a watched series title.

    Checks the pre-built title index first (zero API calls for exact matches),
    then falls back to the search endpoint for unrecognised titles.
    """
    normalized = title.lower()
    if normalized in title_index:
        return title_index[normalized]

    if title in _search_cache:
        return _search_cache[title]

    try:
        data = gql(SEARCH_QUERY, {"search": title})
        mid = data["Media"]["id"]
        _search_cache[title] = mid
        title_index[normalized] = mid  # cache in index for any later duplicates
        return mid
    except Exception:
        _search_cache[title] = None
        return None


def enqueue_outbox_update(conn, anime_id: int, source: str, status: str | None = None,
                           progress: int | None = None, repeat: int | None = None) -> None:
    """Local-first write (issue #100) — replaces the old direct, synchronous
    SaveMediaListEntry call this used to make. Applies the update to library_entries
    immediately (sync_status='pending', so sync_anilist.py's pull-refresh guard —
    upsert_library_entry()'s `WHERE sync_status = 'synced'` clause — can't clobber it,
    the exact same protection issue #18 already gives UI bulk-edits) and enqueues a
    status_sync_outbox row for the app's single shared worker (app/outbox.py) to
    actually deliver to AniList. This is what lets Crunchyroll/Netflix sync stop
    blocking their own fetch/match loop on AniList's rate limit — delivery is now
    decoupled and collectively rate-limited across every outbox source, not just this
    one provider's own sequential calls.

    Deliberately does not commit — runs inside the caller's own transaction, so it
    lands atomically together with that call's own state-tracking write
    (save_nf_state()/save_cr_state()), which does the actual commit. This replaces the
    old "anilist_update() must run BEFORE save_state(), not after" ordering
    requirement (a network call that could fail mid-flight) with something strictly
    safer: recording intent and advancing the watermark now either both happen or
    neither does, in one atomic commit — no network round-trip in between to fail.

    Supersedes (deletes) any not-yet-delivered row for this anime first, same as the
    UI bulk-edit endpoint does, so an older queued update can't fire after a newer one.
    """
    if status is None and progress is None and repeat is None:
        raise ValueError("enqueue_outbox_update requires at least one of status/progress/repeat")

    with conn.cursor() as cur:
        set_clauses = ["sync_status = 'pending'"]
        params: list = []
        if status is not None:
            set_clauses.append("status = %s")
            params.append(status)
        if progress is not None:
            set_clauses.append("progress = %s")
            params.append(progress)
        if repeat is not None:
            set_clauses.append("repeat_count = %s")
            params.append(repeat)
        params.extend([USER_ID, anime_id])
        cur.execute(
            f"UPDATE library_entries SET {', '.join(set_clauses)} WHERE user_id = %s AND anime_id = %s",
            params,
        )
        cur.execute(
            "DELETE FROM status_sync_outbox WHERE user_id = %s AND anime_id = %s AND state IN ('pending', 'failed')",
            (USER_ID, anime_id),
        )
        cur.execute(
            """
            INSERT INTO status_sync_outbox (user_id, anime_id, source, status, progress, repeat_count)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (USER_ID, anime_id, source, status, progress, repeat),
        )


def is_plausible_match(entry: dict, watched_format: str | None,
                        watched_episode_count: int | None) -> bool:
    """Guard against title collisions between AniList's catalog and what was
    actually watched. Netflix/Prime have mostly non-anime catalogs (unlike
    Crunchyroll's anime-only one), and a handful of titles share an exact name
    with an existing anime (live-action Death Note, Cowboy Bebop, One Piece).

    Not airtight against a same-format/similar-length collision — catches the
    cheap, common cases. A wrong progress number is a one-click manual fix, the
    same failure-mode ceiling already accepted for Crunchyroll sync.
    """
    al_format = (entry.get("format") or "").upper()
    al_total = entry.get("total_episodes")

    if watched_format and al_format:
        watched_is_movie = watched_format.upper() == "MOVIE"
        al_is_movie = al_format == "MOVIE"
        if watched_is_movie != al_is_movie:
            return False

    if watched_episode_count and al_total and watched_episode_count > al_total:
        return False

    return True
