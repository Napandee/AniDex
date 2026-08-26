#!/usr/bin/env python3
"""
Prime Video → AniList sync (cookie-replay, incremental, state-aware).

Fetches this user's Prime Video watch history via GET
/api/getWatchHistorySettingsPage, newest-first, stopping once an item at/older
than the stored per-series watermark is reached — same incremental-fetch shape
sync_crunchyroll.py/sync_netflix.py/sync_plex.py already use.

CONFIRMED AGAINST A LIVE ACCOUNT (2026-08-26) via
scripts/dev/probe_primevideo_history.py — see
notes/2026-08-14-netflix-prime-sync-research.md's "Prime Video endpoint —
CONFIRMED" section for the full capture. Endpoint is cookie-authenticated
(PRIMEVIDEO_COOKIE_HEADER, same cookie-paste pattern as Netflix), paginated via
a DynamoDB-shaped `nextToken` cursor, and page 1 comes through this same
endpoint with no `nextToken` at all — no special first-page case needed.

Unlike Netflix's Falcor feed (no absolute episode ordinal — see
sync_netflix.py's module docstring), Prime Video's watch-history response
carries an exact `"Episode N: <title>"` string per watched episode, confirmed
live. This script's aggregation/state/process() logic therefore mirrors
sync_plex.py/sync_crunchyroll.py (max-per-series absolute episode number), not
sync_netflix.py's delta-counting approach.

Response shape (confirmed live): a `widgets[]` entry with
`widgetType == "watch-history"` contains `content.content.titles`, a
newest-first list of date-sections, each holding top-level entries
(`titleType` "movie" or "season"). A movie entry is itself one watched event;
a season entry's `children[]` holds one entry per watched episode, each with
its own exact watch timestamp and `"Episode N: <title>"` text. A season's
`gti` (Amazon's own stable per-season identifier) recurs across multiple
date-sections — and, confirmed live, across multiple *pages* — as episodes
get watched over time, so `gti` (not the display title, which is
inconsistently formatted — see `_parse_season_and_title()`) is the aggregation
key used while walking raw fetch results, before any AniList title resolution
happens.

Title matching: reuses anilist_sync_common.py's find_anilist_id()/
is_plausible_match() — the same title-index-then-search-fallback pair
Crunchyroll/Netflix/Plex already share. Season numbers aren't a separate field
here (unlike Plex's parentIndex) — they're embedded inconsistently in the
season's own display title ("Reacher", "REACHER (TV) - SEASON 01",
"MADE IN ABYSS - Season 1", and at least one observed case of a bare
"Season 3" with no show name at all) — `_parse_season_and_title()` extracts
what it can via regex; a title that doesn't parse cleanly (like the bare
"Season 3" case) simply won't resolve against AniList and gets skipped, same
as CR/Netflix/Plex already do today for anything unmatched.

Exit 0 = success, Exit 1 = fatal error. Matches the other scripts/sync_*.py
scripts' contract.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from anilist_sync_common import (
    enqueue_outbox_update, find_anilist_id, is_plausible_match,
    load_user_list_from_db, resolve_or_create_user_list_entry, season_suffix_candidates,
    seed_search_cache,
)

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])
PRIMEVIDEO_COOKIE_HEADER = os.environ.get("PRIMEVIDEO_COOKIE_HEADER", "")

# Issue #21's pattern (Force Full Resync), wired in from day one per this issue's own
# scope — #20/#21's precedent already exists, no reason to defer this to a follow-up
# the way Plex's did (see sync_plex.py's comment on the same env var).
FORCE_FULL_RESYNC = os.environ.get("FORCE_FULL_RESYNC", "").strip().lower() in ("1", "true", "yes")

MAX_PAGES = 200  # safety cap — matches sync_crunchyroll.py/sync_netflix.py/sync_plex.py

_BASE_URL = "https://www.primevideo.com/api/getWatchHistorySettingsPage"

_SEASON_RE = re.compile(r"^(.*?)[\s,\-]+Season\s*0*(\d+)\b", re.IGNORECASE)
_EPISODE_RE = re.compile(r"^Episode\s+(\d+)\b", re.IGNORECASE)


def log(msg):
    print(f"[primevideosync] user={USER_ID} {msg}", flush=True)


def _emit_result(entries_updated: int, entries_fetched: int, full_pull: bool) -> None:
    """Same SYNC_RESULT contract every scripts/sync_*.py uses — see
    sync_netflix.py's identical helper for why (issue #46)."""
    print(
        f"SYNC_RESULT: {json.dumps({'entries_updated': entries_updated, 'entries_fetched': entries_fetched, 'full_pull': full_pull})}",
        flush=True,
    )


# ── Prime Video history client ──────────────────────────────────────────────

class PrimeVideoHistory:
    """Cookie-authenticated client for GET /api/getWatchHistorySettingsPage."""

    def __init__(self, cookie_header: str):
        cookies = {}
        for part in cookie_header.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                cookies[k.strip()] = v.strip()
        self.client = httpx.Client(
            cookies=cookies,
            headers={
                "accept": "*/*",
                "x-requested-with": "XMLHttpRequest",
                "referer": "https://www.primevideo.com/settings/watch-history",
                "user-agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
            },
            timeout=30,
        )

    def _fetch_page(self, next_token: str | None) -> dict:
        params = {}
        if next_token:
            params["widgetArgs"] = json.dumps({"nextToken": next_token})
        resp = self.client.get(_BASE_URL, params=params)
        resp.raise_for_status()
        return resp.json()

    def fetch_since(self, watermark: datetime | None) -> tuple[list[dict], bool]:
        """Fetch watch history newest-first, stopping once an item at/older than
        `watermark` is hit, or the response has no further `nextToken`. Returns
        (flattened watch events, reached_true_end_of_history) — same contract as
        sync_netflix.py's/sync_plex.py's fetch_since(), see their docstrings for
        what reached_true_end means and why it matters (issue #97).

        Each returned event is a flat dict: {"gti", "display_title", "titleType"
        ("movie"|"episode"), "episode_title_text" (episode only), "time" (epoch
        ms)} — the nested date-section/season/children response shape is walked
        and flattened here so parse_items() doesn't need to know about it."""
        items: list[dict] = []
        reached_true_end = False
        next_token = None

        for _page in range(MAX_PAGES):
            data = self._fetch_page(next_token)
            widgets = data.get("widgets") or []
            wh = next((w for w in widgets if w.get("widgetType") == "watch-history"), None)
            if wh is None:
                reached_true_end = True
                break

            content = ((wh.get("content") or {}).get("content")) or {}
            date_sections = content.get("titles") or []

            hit_watermark = False
            for section in date_sections:
                for entry in section.get("titles") or []:
                    gti = entry.get("gti")
                    display_title = (entry.get("title") or {}).get("text")
                    if entry.get("titleType") == "movie":
                        t = entry.get("time")
                        if t is None:
                            continue
                        watched_at = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
                        if watermark and watched_at <= watermark:
                            hit_watermark = True
                            break
                        items.append({
                            "gti": gti, "display_title": display_title,
                            "titleType": "movie", "episode_title_text": None, "time": t,
                        })
                        continue

                    for child in entry.get("children") or []:
                        t = child.get("time")
                        if t is None:
                            continue
                        watched_at = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
                        if watermark and watched_at <= watermark:
                            hit_watermark = True
                            break
                        items.append({
                            "gti": gti, "display_title": display_title,
                            "titleType": "episode",
                            "episode_title_text": (child.get("title") or {}).get("text"),
                            "time": t,
                        })
                    if hit_watermark:
                        break
                if hit_watermark:
                    break
            if hit_watermark:
                break

            next_token = content.get("nextToken")
            if not next_token:
                reached_true_end = True
                break
        else:
            log(f"WARNING: hit the {MAX_PAGES}-page safety cap without reaching the "
                f"watermark — response shape may not match what this script expects.")

        return items, reached_true_end


def _parse_episode_number(text: str | None) -> int | None:
    if not text:
        return None
    m = _EPISODE_RE.match(text.strip())
    return int(m.group(1)) if m else None


def _parse_season_and_title(display_title: str | None) -> tuple[str, int]:
    """Extracts (base_title, season_number) from Prime Video's inconsistently
    formatted season display text — see module docstring for the observed
    variants. Falls back to (display_title, 1) when no "Season N" pattern is
    found at all, and to (original full string, 1) when the pattern matches
    but leaves nothing usable as a title (the bare "Season 3" case, no show
    name attached) — that case simply won't resolve against AniList and gets
    skipped downstream, same as any other unmatched title."""
    text = (display_title or "").strip()
    if not text:
        return text, 1
    m = _SEASON_RE.match(text)
    if not m:
        return text, 1
    base = m.group(1).strip(" -,")
    if not base:
        return text, 1
    return base, int(m.group(2))


def parse_items(items: list[dict]) -> dict[str, dict]:
    """Returns {gti: {"title", "season", "episode": most_recently_watched_episode,
    "watched_at", "watched_format"}} — same "most recently watched wins, not
    highest episode number" rule as sync_crunchyroll.py's/sync_plex.py's
    parse_items() (so a rewatch started from episode 1 surfaces as episode 1
    for process() to detect). Keyed by `gti` — Amazon's own stable per-season
    identifier — rather than a (title, season) tuple, since the display title
    is inconsistently formatted and not reliably unique the way it is for
    Plex/Crunchyroll."""
    best: dict[str, dict] = {}
    for item in items:
        gti = item.get("gti")
        if not gti:
            continue
        display_title = item.get("display_title")

        if item["titleType"] == "movie":
            ep = 1
            watched_format = "MOVIE"
            title, season = (display_title or "").strip(), 1
        else:
            ep = _parse_episode_number(item.get("episode_title_text"))
            if ep is None:
                continue
            watched_format = "TV"
            title, season = _parse_season_and_title(display_title)

        if not title:
            continue

        watched_at = datetime.fromtimestamp(item["time"] / 1000, tz=timezone.utc)
        existing = best.get(gti)
        if not existing or watched_at > existing["watched_at"]:
            best[gti] = {
                "title": title, "season": season, "episode": ep,
                "watched_at": watched_at, "watched_format": watched_format,
            }

    return best


# ── Postgres ──────────────────────────────────────────────────────────────────

def db_connect():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def ensure_table(conn):
    """Defensive fallback if run against a DB that somehow skipped schema.sql/
    migrations — see sync_crunchyroll.py's ensure_table() for the same rationale."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS primevideo_sync_state (
                user_id                INTEGER NOT NULL,
                anilist_id             INTEGER NOT NULL,
                series_title           TEXT,
                last_seen_episode      INTEGER NOT NULL DEFAULT 0,
                last_seen_watched_at   TIMESTAMPTZ,
                rewatch_in_progress    BOOLEAN NOT NULL DEFAULT FALSE,
                last_synced_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, anilist_id)
            )
        """)
    conn.commit()


def load_pv_state(conn) -> dict[int, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT anilist_id, last_seen_episode, rewatch_in_progress, last_seen_watched_at "
            "FROM primevideo_sync_state WHERE user_id = %s",
            (USER_ID,),
        )
        return {row["anilist_id"]: dict(row) for row in cur.fetchall()}


def save_pv_state(conn, anilist_id: int, title: str, last_ep: int, rewatch: bool):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO primevideo_sync_state (user_id, anilist_id, series_title, last_seen_episode, rewatch_in_progress, last_synced_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (user_id, anilist_id) DO UPDATE SET
                series_title        = EXCLUDED.series_title,
                last_seen_episode   = EXCLUDED.last_seen_episode,
                rewatch_in_progress = EXCLUDED.rewatch_in_progress,
                last_synced_at      = now()
        """, (USER_ID, anilist_id, title, last_ep, rewatch))
    conn.commit()


def save_watermark(conn, anilist_id: int, title: str, watched_at: datetime):
    """Fetch-side watermark bookkeeping only — mirrors sync_crunchyroll.py's/
    sync_plex.py's save_watermark() exactly, see their docstrings for why this
    stays column-disjoint from save_pv_state()."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO primevideo_sync_state (user_id, anilist_id, series_title, last_seen_watched_at, last_synced_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (user_id, anilist_id) DO UPDATE SET
                last_seen_watched_at = GREATEST(
                    COALESCE(primevideo_sync_state.last_seen_watched_at, EXCLUDED.last_seen_watched_at),
                    EXCLUDED.last_seen_watched_at
                )
        """, (USER_ID, anilist_id, title, watched_at))
    conn.commit()


def compute_fetch_watermark(state_map: dict[int, dict]) -> datetime | None:
    values = [s["last_seen_watched_at"] for s in state_map.values() if s.get("last_seen_watched_at")]
    return max(values) if values else None


def load_walk_complete(conn, has_existing_state: bool) -> bool:
    """Whether we've ever confirmed reviewing this account's full Prime Video
    history — same issue #97/#104 pattern as sync_crunchyroll.py/sync_netflix.py/
    sync_plex.py."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM settings WHERE user_id = %s AND key = 'primevideo_walk_complete'",
            (USER_ID,),
        )
        row = cur.fetchone()
    if row is None:
        return has_existing_state
    return row["value"].strip().lower() in ("1", "true", "yes")


def _set_walk_complete(conn, complete: bool):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO settings (user_id, key, value) VALUES (%s, 'primevideo_walk_complete', %s)
            ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value
        """, (USER_ID, "true" if complete else "false"))
    conn.commit()


def load_title_search_cache(conn) -> dict[str, int | None]:
    with conn.cursor() as cur:
        cur.execute("SELECT title, media_id FROM anilist_title_search_cache")
        return {row["title"]: row["media_id"] for row in cur.fetchall()}


def save_title_search_cache_entry(conn, title: str, media_id: int | None):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO anilist_title_search_cache (title, media_id) VALUES (%s, %s)
            ON CONFLICT (title) DO UPDATE SET media_id = EXCLUDED.media_id, cached_at = now()
        """, (title, media_id))
    conn.commit()


# ── Sync logic ────────────────────────────────────────────────────────────────
# process() mirrors sync_plex.py's process() (itself mirroring sync_crunchyroll.py's,
# including the #328 rewatch-clamp fix) verbatim — Prime Video's real absolute episode
# numbers put it in the same "max-aggregated history" shape as Crunchyroll/Plex, not
# Netflix's delta-count shape. Not re-derived here to avoid the three drifting.

def _update(conn, anilist_id: int, **kwargs):
    enqueue_outbox_update(conn, anilist_id, "primevideo", **kwargs)


def process(title: str, pv_ep: int, entry: dict, pv_state: dict | None, conn) -> str:
    status = entry["status"]
    al_ep = entry["progress"]
    repeat = entry["repeat"]
    total = entry["total_episodes"]
    anilist_id = entry["anilist_id"]

    last_ep = pv_state["last_seen_episode"] if pv_state else al_ep
    rewatch_active = pv_state["rewatch_in_progress"] if pv_state else False

    if status is None:
        _update(conn, anilist_id, progress=pv_ep, status="WATCHING")
        save_pv_state(conn, anilist_id, title, pv_ep, False)
        return f"new AniList entry created → WATCHING ep {pv_ep}"

    if pv_state is None and status == "COMPLETED":
        save_pv_state(conn, anilist_id, title, pv_ep, False)
        return "first-sync (COMPLETED) — state recorded, no change"

    if status == "REPEATING" and not rewatch_active:
        if pv_ep > al_ep:
            _update(conn, anilist_id, progress=pv_ep)
            save_pv_state(conn, anilist_id, title, pv_ep, True)
            return f"rewatch detected (already REPEATING) → progress {al_ep} → {pv_ep}"
        save_pv_state(conn, anilist_id, title, pv_ep, True)
        return "rewatch detected (already REPEATING) — state recorded"

    if status == "COMPLETED" and pv_ep < (last_ep or total or 999) and not rewatch_active:
        _update(conn, anilist_id, progress=pv_ep, status="REPEATING")
        save_pv_state(conn, anilist_id, title, pv_ep, True)
        return f"rewatch started → REPEATING ep {pv_ep}"

    if rewatch_active and pv_ep < last_ep:
        _update(conn, anilist_id, progress=pv_ep)
        save_pv_state(conn, anilist_id, title, pv_ep, True)
        return f"new rewatch pass detected (was at {last_ep}) → progress reset to {pv_ep}"

    if pv_ep <= last_ep and not rewatch_active:
        if pv_ep <= al_ep:
            save_pv_state(conn, anilist_id, title, last_ep, rewatch_active)
            return f"no change (Prime Video={pv_ep}, last_seen={last_ep})"

    if rewatch_active and total and pv_ep >= total:
        _update(conn, anilist_id, progress=pv_ep, status="COMPLETED", repeat=repeat + 1)
        save_pv_state(conn, anilist_id, title, pv_ep, False)
        return f"rewatch complete → COMPLETED (repeat #{repeat + 1})"

    if rewatch_active and pv_ep > al_ep:
        _update(conn, anilist_id, progress=pv_ep)
        save_pv_state(conn, anilist_id, title, pv_ep, True)
        return f"rewatch progress {al_ep} → {pv_ep}"

    if status == "DROPPED" and pv_ep > last_ep:
        _update(conn, anilist_id, progress=pv_ep, status="CURRENT")
        save_pv_state(conn, anilist_id, title, pv_ep, False)
        return f"resumed after DROP → CURRENT ep {pv_ep}"

    if pv_ep > al_ep:
        _update(conn, anilist_id, progress=pv_ep)
        save_pv_state(conn, anilist_id, title, pv_ep, False)
        return f"progress {al_ep} → {pv_ep}"

    save_pv_state(conn, anilist_id, title, max(pv_ep, last_ep), rewatch_active)
    return f"AniList ({al_ep}) already at or ahead of Prime Video ({pv_ep})"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Starting Prime Video → AniList sync")

    if not PRIMEVIDEO_COOKIE_HEADER:
        log("ERROR: Prime Video credentials not configured (PRIMEVIDEO_COOKIE_HEADER)")
        sys.exit(1)

    conn = db_connect()
    ensure_table(conn)
    state_map = load_pv_state(conn)
    log(f"Loaded Prime Video sync state for {len(state_map)} series")

    walk_complete = load_walk_complete(conn, has_existing_state=bool(state_map))
    if walk_complete and FORCE_FULL_RESYNC:
        log("FORCE_FULL_RESYNC set — starting a fresh full walk (a previous walk had already completed)")
        _set_walk_complete(conn, False)
        walk_complete = False
        watermark = None
    elif walk_complete:
        watermark = compute_fetch_watermark(state_map)
    else:
        log("Full walk not yet complete — re-walking full history this run")
        watermark = None
    full_pull = watermark is None
    log(f"Fetching Prime Video watch history since {watermark or '(no watermark — full walk)'}")

    client = PrimeVideoHistory(PRIMEVIDEO_COOKIE_HEADER)
    try:
        raw_items, reached_true_end = client.fetch_since(watermark)
    except Exception as e:
        log(f"ERROR: Prime Video fetch failed: {e}")
        conn.close()
        sys.exit(1)
    log(f"Fetched {len(raw_items)} new history rows")

    if not raw_items:
        if reached_true_end:
            _set_walk_complete(conn, True)
            log("Reached true end of Prime Video history — full walk marked complete")
        log("No new activity — nothing to do")
        conn.close()
        _emit_result(0, len(raw_items), full_pull)
        sys.exit(0)

    watched_by_gti = parse_items(raw_items)
    log(f"{len(watched_by_gti)} unique season/movie(s) touched since last sync")

    user_list, title_index = load_user_list_from_db()
    log(f"Loaded {len(user_list)} AniList entries, {len(title_index)} title variants indexed (from local mirror)")

    title_search_cache = load_title_search_cache(conn)
    seed_search_cache(title_search_cache)
    log(f"Loaded {len(title_search_cache)} cached AniList title-search results")

    updated = skipped = no_change = index_hits = search_hits = 0

    for gti, watched in sorted(watched_by_gti.items()):
        title = watched["title"]
        season = watched["season"]
        normalized = title.lower()
        candidates = season_suffix_candidates(title, season) if season > 1 else []
        in_index_before = normalized in title_index or any(c.lower() in title_index for c in candidates)

        media_id = find_anilist_id(title, title_index, season_number=season)
        if not in_index_before and title not in title_search_cache:
            save_title_search_cache_entry(conn, title, media_id)
            title_search_cache[title] = media_id
        if in_index_before and media_id:
            index_hits += 1
        elif media_id:
            search_hits += 1
        if not media_id:
            log(f"  ✗ No AniList match: '{title}'" + (f" (season {season})" if season > 1 else ""))
            skipped += 1
            continue

        decision = resolve_or_create_user_list_entry(media_id, title, user_list, full_pull, conn)
        if decision == "skip":
            log(f"  ✗ Not in your AniList: '{title}'")
            skipped += 1
            continue
        if decision == "create":
            log(f"  + Not yet tracked — creating a new AniList entry: '{title}'")

        entry = dict(user_list[media_id])
        entry["anilist_id"] = media_id

        if not is_plausible_match(entry, watched["watched_format"], watched["episode"]):
            log(f"  ✗ Implausible match, skipping: '{title}' "
                f"(AniList format={entry.get('format')}, total_eps={entry.get('total_episodes')}; "
                f"watched format={watched['watched_format']}, ep={watched['episode']})")
            skipped += 1
            continue

        pv_state = state_map.get(media_id)
        try:
            result = process(title, watched["episode"], entry, pv_state, conn)
            log(f"  '{title}': {result}")
            save_watermark(conn, media_id, title, watched["watched_at"])
            if "→" in result:
                updated += 1
            else:
                no_change += 1
        except Exception as e:
            log(f"  ERROR processing '{title}': {e}")
            skipped += 1

    if reached_true_end:
        _set_walk_complete(conn, True)
        log("Reached true end of Prime Video history — full walk marked complete")

    conn.close()
    log(f"Done — {updated} updated, {no_change} unchanged, {skipped} skipped/unmatched "
        f"({index_hits} index hits, {search_hits} API searches)")
    _emit_result(updated, len(raw_items), full_pull)


if __name__ == "__main__":
    main()
