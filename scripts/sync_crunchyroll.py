#!/usr/bin/env python3
"""
Crunchyroll → AniList sync (safe, state-aware).

Reads the history.json produced by crunchyexporter-cli fetch, compares against
the last-known state in Postgres, then updates AniList with the correct logic:

  CURRENT / PAUSED   + CR ahead          → advance progress
  DROPPED            + CR ahead          → advance progress + set CURRENT
  COMPLETED          + CR ep < last-seen → set REPEATING, advance progress (rewatch started)
  REPEATING (no state recorded)          → record rewatch as active, advance progress if needed
  REPEATING          + CR ahead          → advance progress
  REPEATING          + CR >= total eps   → set COMPLETED, increment repeat counter

Note: CR history is max-aggregated (highest episode ever watched per series).
A rewatch starting from ep 1 won't lower cr_ep unless old episodes age out of
history. The REPEATING handler is therefore the reliable rewatch detection path
— the user changes status to REPEATING in the app and sync picks it up on next
run; the COMPLETED+drop-below-last-seen path catches history-trimming edge cases.

Never goes backwards on progress. Never touches score or notes.

Exit 0 = success, Exit 1 = fatal error.
"""

import json
import os
import sys
import time

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
ANILIST_TOKEN = os.environ["ANILIST_TOKEN"]
ANILIST_USERNAME = os.environ["ANILIST_USERNAME"]
HISTORY_PATH = os.environ.get("HISTORY_PATH", "/crunchyexporter/data/history.json")
ANILIST_API = "https://graphql.anilist.co"


def log(msg):
    print(f"[crunchysync] {msg}", flush=True)


# ── History parsing ───────────────────────────────────────────────────────────

def load_history(path: str) -> dict[str, int]:
    """Parse history.json → {series_title: highest_episode_watched}."""
    with open(path) as f:
        data = json.load(f)

    # crunchyexporter-cli stores episodes as a dict keyed by episode_id,
    # optionally wrapped under an "episodes" key. Handle both.
    if isinstance(data, dict) and "episodes" in data:
        raw = data["episodes"]
        items = raw.values() if isinstance(raw, dict) else raw
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.values()
    else:
        return {}

    best: dict[str, int] = {}
    for item in items:
        title = (item.get("series_title") or "").strip()
        if not title:
            continue
        try:
            ep = int(float(item.get("episode_number") or 0))
        except (ValueError, TypeError):
            ep = 0
        if ep == 0:
            continue
        if title not in best or ep > best[title]:
            best[title] = ep

    return best


# ── Postgres ──────────────────────────────────────────────────────────────────

def db_connect():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cr_sync_state (
                anilist_id            INTEGER PRIMARY KEY,
                series_title          TEXT,
                last_seen_episode     INTEGER NOT NULL DEFAULT 0,
                rewatch_in_progress   BOOLEAN NOT NULL DEFAULT FALSE,
                last_synced_at        TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    conn.commit()


def load_cr_state(conn) -> dict[int, dict]:
    """Return {anilist_id: {last_seen_episode, rewatch_in_progress}}."""
    with conn.cursor() as cur:
        cur.execute("SELECT anilist_id, last_seen_episode, rewatch_in_progress FROM cr_sync_state")
        return {row["anilist_id"]: dict(row) for row in cur.fetchall()}


def save_cr_state(conn, anilist_id: int, title: str, last_ep: int, rewatch: bool):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO cr_sync_state (anilist_id, series_title, last_seen_episode, rewatch_in_progress, last_synced_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (anilist_id) DO UPDATE SET
                series_title        = EXCLUDED.series_title,
                last_seen_episode   = EXCLUDED.last_seen_episode,
                rewatch_in_progress = EXCLUDED.rewatch_in_progress,
                last_synced_at      = now()
        """, (anilist_id, title, last_ep, rewatch))
    conn.commit()


# ── AniList ───────────────────────────────────────────────────────────────────

def gql(query: str, variables: dict | None = None, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.post(
        ANILIST_API,
        json={"query": query, "variables": variables or {}},
        headers=headers,
        timeout=30,
    )
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
    title { romaji english }
  }
}
"""

UPDATE_MUTATION = """
mutation ($mediaId: Int!, $progress: Int, $status: MediaListStatus, $repeat: Int) {
  SaveMediaListEntry(mediaId: $mediaId, progress: $progress, status: $status, repeat: $repeat) {
    id progress status repeat
  }
}
"""


def fetch_user_list() -> tuple[dict[int, dict], dict[str, int]]:
    """Fetch all the user's AniList entries in one call.

    Returns (entries_by_id, title_index) where title_index maps
    lowercased romaji/english titles → mediaId for fast CR title matching.
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
                "title": english or romaji or "",
            }

            for t in (romaji, english):
                if t:
                    title_index[t.lower()] = mid

    return entries, title_index


_search_cache: dict[str, int | None] = {}


def find_anilist_id(title: str, title_index: dict[str, int]) -> int | None:
    """Return AniList media ID for a CR series title.

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


def anilist_update(media_id: int, progress: int | None = None,
                   status: str | None = None, repeat: int | None = None):
    variables: dict = {"mediaId": media_id}
    if progress is not None:
        variables["progress"] = progress
    if status is not None:
        variables["status"] = status
    if repeat is not None:
        variables["repeat"] = repeat
    gql(UPDATE_MUTATION, variables, token=ANILIST_TOKEN)
    time.sleep(0.7)  # stay under AniList's 90 req/min


# ── Sync logic ────────────────────────────────────────────────────────────────

def process(title: str, cr_ep: int, entry: dict, cr_state: dict | None,
            conn) -> str:
    """
    Apply update logic for one series. Returns a short description of action taken.
    cr_state may be None on first sync for this series.
    """
    status = entry["status"]
    al_ep = entry["progress"]
    repeat = entry["repeat"]
    total = entry["total_episodes"]
    al_id = None  # resolved by caller; passed via entry for convenience
    anilist_id = entry["anilist_id"]

    last_ep = cr_state["last_seen_episode"] if cr_state else al_ep
    rewatch_active = cr_state["rewatch_in_progress"] if cr_state else False

    # ── First-time seeing a COMPLETED series in CR history ────────────────────
    # Without prior state we can't safely distinguish "rewatch" from "first sync".
    # Record state and do nothing — next sync will have a baseline.
    if cr_state is None and status == "COMPLETED":
        save_cr_state(conn, anilist_id, title, cr_ep, False)
        return "first-sync (COMPLETED) — state recorded, no change"

    # ── AniList status already REPEATING but rewatch not recorded in state ────
    # Handles: user changes status to REPEATING in the app/AniList before sync
    # runs. Set rewatch_active so subsequent syncs advance progress correctly.
    if status == "REPEATING" and not rewatch_active:
        save_cr_state(conn, anilist_id, title, cr_ep, True)
        if cr_ep > al_ep:
            anilist_update(anilist_id, progress=cr_ep)
            return f"rewatch detected (already REPEATING) → progress {al_ep} → {cr_ep}"
        return "rewatch detected (already REPEATING) — state recorded"

    # ── Rewatch: COMPLETED but CR episode dropped below last-seen ────────────
    # Must come BEFORE the no-change guard: cr_ep < last_ep satisfies that guard
    # and would short-circuit before we ever detect the rewatch.
    if status == "COMPLETED" and cr_ep < (last_ep or total or 999) and not rewatch_active:
        anilist_update(anilist_id, progress=cr_ep, status="REPEATING")
        save_cr_state(conn, anilist_id, title, cr_ep, True)
        return f"rewatch started → REPEATING ep {cr_ep}"

    # ── No progress since last sync ───────────────────────────────────────────
    if cr_ep <= last_ep and not rewatch_active:
        if cr_ep > al_ep:
            # AniList is behind but we already processed this — shouldn't happen often
            pass
        else:
            save_cr_state(conn, anilist_id, title, last_ep, rewatch_active)
            return f"no change (CR={cr_ep}, last_seen={last_ep})"

    # ── Rewatch completion: REPEATING and reached total episodes ─────────────
    if rewatch_active and total and cr_ep >= total:
        anilist_update(anilist_id, progress=cr_ep, status="COMPLETED", repeat=repeat + 1)
        save_cr_state(conn, anilist_id, title, cr_ep, False)
        return f"rewatch complete → COMPLETED (repeat #{repeat + 1})"

    # ── Progress advance for active rewatch ───────────────────────────────────
    if rewatch_active and cr_ep > al_ep:
        anilist_update(anilist_id, progress=cr_ep)
        save_cr_state(conn, anilist_id, title, cr_ep, True)
        return f"rewatch progress {al_ep} → {cr_ep}"

    # ── DROPPED: user picked it back up ──────────────────────────────────────
    if status == "DROPPED" and cr_ep > last_ep:
        anilist_update(anilist_id, progress=cr_ep, status="CURRENT")
        save_cr_state(conn, anilist_id, title, cr_ep, False)
        return f"resumed after DROP → CURRENT ep {cr_ep}"

    # ── Normal progress advance (CURRENT, PAUSED) ─────────────────────────────
    if cr_ep > al_ep:
        anilist_update(anilist_id, progress=cr_ep)
        save_cr_state(conn, anilist_id, title, cr_ep, False)
        return f"progress {al_ep} → {cr_ep}"

    # Nothing to do
    save_cr_state(conn, anilist_id, title, max(cr_ep, last_ep), rewatch_active)
    return f"AniList ({al_ep}) already at or ahead of CR ({cr_ep})"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Starting Crunchyroll → AniList sync")

    if not os.path.exists(HISTORY_PATH):
        log(f"ERROR: history file not found at {HISTORY_PATH}")
        sys.exit(1)

    log(f"Loading history from {HISTORY_PATH}...")
    history = load_history(HISTORY_PATH)
    log(f"Parsed {len(history)} unique series from CR history")

    if not history:
        log("No history found — nothing to do")
        sys.exit(0)

    log("Fetching AniList library (one call)...")
    user_list, title_index = fetch_user_list()
    log(f"Loaded {len(user_list)} AniList entries, {len(title_index)} title variants indexed")

    conn = db_connect()
    ensure_table(conn)
    cr_state_map = load_cr_state(conn)
    log(f"Loaded CR sync state for {len(cr_state_map)} series")

    updated = skipped = no_change = index_hits = search_hits = 0

    for title, cr_ep in sorted(history.items()):
        normalized = title.lower()
        in_index_before = normalized in title_index
        media_id = find_anilist_id(title, title_index)
        if in_index_before and media_id:
            index_hits += 1
        elif media_id:
            search_hits += 1
        if not media_id:
            log(f"  ✗ No AniList match: '{title}'")
            skipped += 1
            continue

        if media_id not in user_list:
            log(f"  ✗ Not in your AniList: '{title}'")
            skipped += 1
            continue

        entry = dict(user_list[media_id])
        entry["anilist_id"] = media_id
        cr_state = cr_state_map.get(media_id)

        try:
            result = process(title, cr_ep, entry, cr_state, conn)
            log(f"  '{title}': {result}")
            if "→" in result:
                updated += 1
            else:
                no_change += 1
        except Exception as e:
            log(f"  ERROR processing '{title}': {e}")
            skipped += 1

    conn.close()
    log(f"Done — {updated} updated, {no_change} unchanged, {skipped} skipped/unmatched "
        f"({index_hits} index hits, {search_hits} API searches)")


if __name__ == "__main__":
    main()
