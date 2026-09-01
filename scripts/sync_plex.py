#!/usr/bin/env python3
"""
Plex → AniList sync (server-token, incremental, state-aware).

Fetches this user's Plex watch history via GET /status/sessions/history/all on
their own Plex Media Server (sort=viewedAt:desc, paginated via the standard
X-Plex-Container-Start/X-Plex-Container-Size headers, stopping once an item
at/older than the stored per-series watermark is reached — Plex's own
viewedAt> filter does the server-side cutoff for the common case, matching
the incremental-fetch shape sync_crunchyroll.py/sync_netflix.py already use).

Unlike Netflix's Falcor feed (no absolute episode ordinal — see
sync_netflix.py's module docstring), Plex history items carry real
season/episode numbers (`parentIndex`/`index`, standard Plex video metadata
fields present on any episode object) the same way Crunchyroll's episode_metadata
does. This script's aggregation/state/process() logic therefore mirrors
sync_crunchyroll.py almost exactly (max-per-series absolute episode number),
not sync_netflix.py's delta-counting approach.

Title matching: reuses anilist_sync_common.py's find_anilist_id()/
is_plausible_match() — the same title-index-then-search-fallback pair
Crunchyroll/Netflix already share — keyed on grandparentTitle (episodes) or
title (movies), with season_number passed through for AniList's "2nd Season"
naming convention (issue #159's fix, general enough to reuse here). Title
matching is the whole matching path for now — a Plex library item's `Guid`
list can carry an AniDB/MAL id (only present if the user has an anime-
specific metadata agent like HAMA or MyAnimeList.bundle installed) that
would be a strictly better match signal, but that's deliberately not wired
in for v1 pending an AniDB/MAL → AniList id mapping table; see
notes/2026-08-19-plex-sync-research.md, section 3.

Auth: X-Plex-Token (server-scoped, from the OAuth PIN connect flow in
app/plex_auth.py — see that module's docstring for why this isn't a
cookie-paste credential like Crunchyroll/Netflix) against PLEX_SERVER_BASE_URL.

CONFIDENCE NOTE (see notes/2026-08-19-plex-sync-research.md): the endpoint,
auth header, sort/watermark params, and field list below are all confirmed by
reading python-plexapi's actual source, not just documentation — but nobody
has run this against a real Plex server yet. The exact JSON response shape
(this app requests `Accept: application/json`, matching how
sync_crunchyroll.py/sync_netflix.py already prefer JSON APIs over hand-parsing
XML) is inferred from the well-established 1:1 XML-attribute-to-JSON-key
mapping Plex's API uses elsewhere, not directly observed. Flag anything below
that turns out wrong on first live run.

Exit 0 = success, Exit 1 = fatal error. Matches the other scripts/sync_*.py
scripts' contract.
"""

import json
import os
import sys
from datetime import datetime, timezone

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from anilist_sync_common import (
    enqueue_outbox_update, find_anilist_id, is_plausible_match,
    load_user_list_from_db, load_walk_complete, resolve_or_create_user_list_entry,
    season_suffix_candidates, seed_search_cache, set_walk_complete,
)

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])
PLEX_SERVER_TOKEN = os.environ.get("PLEX_SERVER_TOKEN", "")
PLEX_SERVER_BASE_URL = os.environ.get("PLEX_SERVER_BASE_URL", "").rstrip("/")

# Issue #387, Part 2 — same DRY_RUN pattern sync_netflix.py already had (this script
# had none until now — see anilist_sync_common.py's load_walk_complete() docstring for
# why every provider script needing this matters, not just Prime Video).
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

# Issue #21's pattern (Force Full Resync) — not yet wired into a UI button for Plex
# (see issue #153's "Out of scope"), but the sync script supports it from day one so
# that follow-up is just wiring, not new sync logic, same precedent as #21 extending
# #20's button to a second provider.
FORCE_FULL_RESYNC = os.environ.get("FORCE_FULL_RESYNC", "").strip().lower() in ("1", "true", "yes")

PAGE_SIZE = 100
MAX_PAGES = 200  # safety cap — matches sync_crunchyroll.py/sync_netflix.py

# Issue #153's research (notes/2026-08-19-plex-sync-research.md §3) found that an
# anime-specific Plex agent (HAMA / MyAnimeList.bundle) can put an AniDB/MAL id on
# an item's Guid list — a strictly better match signal than title-matching where
# present. Deliberately not wired in for v1: mapping AniDB/MAL ids to AniList ids
# needs its own mapping table, which the research explicitly declined to add as a
# dependency for this pass (same call the Netflix/Prime research made). Title
# matching below is the whole matching path for now; a real follow-up issue should
# add the agent-id fast path on top of it, not the other way round.


def log(msg):
    print(f"[plexsync] user={USER_ID} {msg}", flush=True)


def _emit_result(entries_updated: int, entries_fetched: int, full_pull: bool) -> None:
    """Same SYNC_RESULT contract every scripts/sync_*.py uses — see
    sync_netflix.py's identical helper for why (issue #46). Not emitted in
    DRY_RUN — that mode is a local investigation tool, never invoked through
    run_full_sync.py."""
    if DRY_RUN:
        return
    print(
        f"SYNC_RESULT: {json.dumps({'entries_updated': entries_updated, 'entries_fetched': entries_fetched, 'full_pull': full_pull})}",
        flush=True,
    )


# ── Plex history client ─────────────────────────────────────────────────────────

class PlexHistory:
    """Server-token-authenticated client for GET /status/sessions/history/all."""

    def __init__(self, base_url: str, server_token: str):
        self.base_url = base_url
        self.client = httpx.Client(
            headers={"Accept": "application/json", "X-Plex-Token": server_token},
            timeout=30,
        )

    def fetch_since(self, watermark: datetime | None) -> tuple[list[dict], bool]:
        """Fetch watch history newest-first, stopping once an item at/older than
        `watermark` is hit, or a short page signals the end of history. Returns
        (raw Metadata dicts, reached_true_end_of_history) — same contract as
        sync_netflix.py's fetch_since(), see its docstring for what
        reached_true_end means and why it matters (issue #97)."""
        items: list[dict] = []
        reached_true_end = False
        params = {"sort": "viewedAt:desc"}
        if watermark:
            params["viewedAt>"] = str(int(watermark.timestamp()))

        for page in range(MAX_PAGES):
            resp = self.client.get(
                f"{self.base_url}/status/sessions/history/all",
                params=params,
                headers={
                    "X-Plex-Container-Start": str(page * PAGE_SIZE),
                    "X-Plex-Container-Size": str(PAGE_SIZE),
                },
            )
            resp.raise_for_status()
            page_items = (resp.json().get("MediaContainer") or {}).get("Metadata") or []
            if not page_items:
                reached_true_end = True
                break
            items.extend(page_items)
            if len(page_items) < PAGE_SIZE:
                reached_true_end = True
                break
        else:
            log(f"WARNING: hit the {MAX_PAGES}-page safety cap without reaching the "
                f"watermark — response shape may not match what this script expects.")

        return items, reached_true_end


def _item_watched_at(item: dict) -> datetime | None:
    ts = item.get("viewedAt")
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def _is_episode(item: dict) -> bool:
    return item.get("type") == "episode"


def parse_items(items: list[dict]) -> dict[tuple[str, int], dict]:
    """Returns {(series_title, season_number): {"episode": most_recently_watched_episode,
    "watched_at": datetime}} — same "most recently watched wins, not highest
    episode number" rule as sync_crunchyroll.py's parse_items() (so a rewatch
    started from episode 1 surfaces as episode 1 for process() to detect), keyed
    by (title, season) for the same reason issue #159 keyed CR's parser that way:
    two seasons of the same franchise watched in one sync window must not
    collapse into a single entry.

    season_number defaults to 1 when parentIndex is absent/non-numeric (movies
    have no parentIndex at all)."""
    best: dict[tuple[str, int], dict] = {}
    for item in items:
        is_ep = _is_episode(item)
        title = ((item.get("grandparentTitle") if is_ep else item.get("title")) or "").strip()
        if not title:
            continue
        try:
            ep = int(item.get("index") or 0) if is_ep else 1
        except (ValueError, TypeError):
            ep = 0
        if ep == 0:
            continue
        try:
            season = int(item.get("parentIndex") or 1) if is_ep else 1
        except (ValueError, TypeError):
            season = 1
        if season < 1:
            season = 1

        key = (title, season)
        watched_at = _item_watched_at(item)
        if not watched_at:
            continue
        existing = best.get(key)
        if not existing or watched_at > existing["watched_at"]:
            best[key] = {
                "episode": ep,
                "watched_at": watched_at,
                "watched_format": "MOVIE" if not is_ep else "TV",
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
            CREATE TABLE IF NOT EXISTS plex_sync_state (
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


def load_plex_state(conn) -> dict[int, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT anilist_id, last_seen_episode, rewatch_in_progress, last_seen_watched_at "
            "FROM plex_sync_state WHERE user_id = %s",
            (USER_ID,),
        )
        return {row["anilist_id"]: dict(row) for row in cur.fetchall()}


def save_plex_state(conn, anilist_id: int, title: str, last_ep: int, rewatch: bool):
    if conn is None:  # DRY_RUN — no DB writes at all, matching the rest of that mode
        return
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO plex_sync_state (user_id, anilist_id, series_title, last_seen_episode, rewatch_in_progress, last_synced_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (user_id, anilist_id) DO UPDATE SET
                series_title        = EXCLUDED.series_title,
                last_seen_episode   = EXCLUDED.last_seen_episode,
                rewatch_in_progress = EXCLUDED.rewatch_in_progress,
                last_synced_at      = now()
        """, (USER_ID, anilist_id, title, last_ep, rewatch))
    conn.commit()


def save_watermark(conn, anilist_id: int, title: str, watched_at: datetime):
    """Fetch-side watermark bookkeeping only — mirrors sync_crunchyroll.py's
    save_watermark() exactly, see its docstring for why this stays column-disjoint
    from save_plex_state()."""
    if conn is None:  # DRY_RUN — no DB writes at all, matching the rest of that mode
        return
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO plex_sync_state (user_id, anilist_id, series_title, last_seen_watched_at, last_synced_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (user_id, anilist_id) DO UPDATE SET
                last_seen_watched_at = GREATEST(
                    COALESCE(plex_sync_state.last_seen_watched_at, EXCLUDED.last_seen_watched_at),
                    EXCLUDED.last_seen_watched_at
                )
        """, (USER_ID, anilist_id, title, watched_at))
    conn.commit()


def compute_fetch_watermark(state_map: dict[int, dict]) -> datetime | None:
    values = [s["last_seen_watched_at"] for s in state_map.values() if s.get("last_seen_watched_at")]
    return max(values) if values else None


def load_title_search_cache(conn) -> dict[str, int | None]:
    """Global (not per-user) AniList title-search cache (issue #115). `conn` may
    be None in DRY_RUN, matching that mode's "no DB reads at all" framing."""
    if conn is None:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT title, media_id FROM anilist_title_search_cache")
        return {row["title"]: row["media_id"] for row in cur.fetchall()}


def save_title_search_cache_entry(conn, title: str, media_id: int | None):
    if conn is None:  # DRY_RUN — no DB writes at all, matching the rest of that mode
        return
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO anilist_title_search_cache (title, media_id) VALUES (%s, %s)
            ON CONFLICT (title) DO UPDATE SET media_id = EXCLUDED.media_id, cached_at = now()
        """, (title, media_id))
    conn.commit()


# ── Sync logic ────────────────────────────────────────────────────────────────
# process() mirrors sync_crunchyroll.py's process() (including the #328 rewatch-
# clamp fix) near-verbatim — Plex's real absolute episode numbers put it in the
# same "max-aggregated history" shape as Crunchyroll, not Netflix's delta-count
# shape. See sync_crunchyroll.py's process() docstring/comments for the full
# reasoning behind each branch; not re-derived here to avoid the two drifting.

def _update(conn, anilist_id: int, **kwargs):
    """enqueue_outbox_update(), routed through DRY_RUN — see its module-level
    docstring. Issue #100 — no longer pushes to AniList directly/synchronously;
    enqueues to status_sync_outbox for the app's shared outbox worker to
    deliver."""
    if DRY_RUN:
        log(f"    [dry-run] would enqueue outbox update({anilist_id}, {kwargs})")
    else:
        enqueue_outbox_update(conn, anilist_id, "plex", **kwargs)


def _save_state(conn, anilist_id: int, title: str, last_ep: int, rewatch: bool):
    """save_plex_state(), routed through DRY_RUN for a consistent log line —
    see _update()'s docstring. save_plex_state() itself already guards conn is
    None (safe to call directly), this wrapper exists purely so process()'s
    DRY_RUN output says what it would have saved, matching sync_netflix.py's
    pattern."""
    if DRY_RUN:
        log(f"    [dry-run] would save state: anilist_id={anilist_id} last_ep={last_ep} rewatch={rewatch}")
    else:
        save_plex_state(conn, anilist_id, title, last_ep, rewatch)


def process(title: str, plex_ep: int, entry: dict, plex_state: dict | None, conn) -> str:
    status = entry["status"]
    al_ep = entry["progress"]
    repeat = entry["repeat"]
    total = entry["total_episodes"]
    anilist_id = entry["anilist_id"]

    last_ep = plex_state["last_seen_episode"] if plex_state else al_ep
    rewatch_active = plex_state["rewatch_in_progress"] if plex_state else False

    if status is None:
        _update(conn, anilist_id, progress=plex_ep, status="WATCHING")
        _save_state(conn, anilist_id, title, plex_ep, False)
        return f"new AniList entry created → WATCHING ep {plex_ep}"

    if plex_state is None and status == "COMPLETED":
        _save_state(conn, anilist_id, title, plex_ep, False)
        return "first-sync (COMPLETED) — state recorded, no change"

    if status == "REPEATING" and not rewatch_active:
        if plex_ep > al_ep:
            _update(conn, anilist_id, progress=plex_ep)
            _save_state(conn, anilist_id, title, plex_ep, True)
            return f"rewatch detected (already REPEATING) → progress {al_ep} → {plex_ep}"
        _save_state(conn, anilist_id, title, plex_ep, True)
        return "rewatch detected (already REPEATING) — state recorded"

    if status == "COMPLETED" and plex_ep < (last_ep or total or 999) and not rewatch_active:
        _update(conn, anilist_id, progress=plex_ep, status="REPEATING")
        _save_state(conn, anilist_id, title, plex_ep, True)
        return f"rewatch started → REPEATING ep {plex_ep}"

    if rewatch_active and plex_ep < last_ep:
        _update(conn, anilist_id, progress=plex_ep)
        _save_state(conn, anilist_id, title, plex_ep, True)
        return f"new rewatch pass detected (was at {last_ep}) → progress reset to {plex_ep}"

    if plex_ep <= last_ep and not rewatch_active:
        if plex_ep <= al_ep:
            _save_state(conn, anilist_id, title, last_ep, rewatch_active)
            return f"no change (Plex={plex_ep}, last_seen={last_ep})"

    if rewatch_active and total and plex_ep >= total:
        _update(conn, anilist_id, progress=plex_ep, status="COMPLETED", repeat=repeat + 1)
        _save_state(conn, anilist_id, title, plex_ep, False)
        return f"rewatch complete → COMPLETED (repeat #{repeat + 1})"

    if rewatch_active and plex_ep > al_ep:
        _update(conn, anilist_id, progress=plex_ep)
        _save_state(conn, anilist_id, title, plex_ep, True)
        return f"rewatch progress {al_ep} → {plex_ep}"

    if status == "DROPPED" and plex_ep > last_ep:
        _update(conn, anilist_id, progress=plex_ep, status="CURRENT")
        _save_state(conn, anilist_id, title, plex_ep, False)
        return f"resumed after DROP → CURRENT ep {plex_ep}"

    if plex_ep > al_ep:
        _update(conn, anilist_id, progress=plex_ep)
        _save_state(conn, anilist_id, title, plex_ep, False)
        return f"progress {al_ep} → {plex_ep}"

    _save_state(conn, anilist_id, title, max(plex_ep, last_ep), rewatch_active)
    return f"AniList ({al_ep}) already at or ahead of Plex ({plex_ep})"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Starting Plex → AniList sync")

    if not PLEX_SERVER_TOKEN or not PLEX_SERVER_BASE_URL:
        log("ERROR: Plex credentials not configured (PLEX_SERVER_TOKEN / PLEX_SERVER_BASE_URL)")
        sys.exit(1)

    if DRY_RUN:
        # No DB touched at all in dry-run — not even the CREATE TABLE IF NOT EXISTS
        # fallback. Treated as a from-scratch first sync (no watermark), which is
        # also the most useful dry-run shape: it exercises the fetch/parse/match/
        # process path (including the create-decision plausibility gate, issue
        # #387) against your full real history without writing anything.
        log("[dry-run] skipping database entirely — no reads, no writes")
        conn = None
        state_map: dict[int, dict] = {}
    else:
        conn = db_connect()
        ensure_table(conn)
        state_map = load_plex_state(conn)
        log(f"Loaded Plex sync state for {len(state_map)} series")

    walk_complete = load_walk_complete(conn, "plex", USER_ID)
    if walk_complete and FORCE_FULL_RESYNC:
        log("FORCE_FULL_RESYNC set — starting a fresh full walk (a previous walk had already completed)")
        set_walk_complete(conn, "plex", USER_ID, False)
        walk_complete = False
        watermark = None
    elif walk_complete:
        watermark = compute_fetch_watermark(state_map)
    else:
        log("Full walk not yet complete — re-walking full history this run")
        watermark = None
    full_pull = watermark is None
    log(f"Fetching Plex watch history since {watermark or '(no watermark — full walk)'}")

    client = PlexHistory(PLEX_SERVER_BASE_URL, PLEX_SERVER_TOKEN)
    try:
        raw_items, reached_true_end = client.fetch_since(watermark)
    except Exception as e:
        log(f"ERROR: Plex fetch failed: {e}")
        if conn:
            conn.close()
        sys.exit(1)
    log(f"Fetched {len(raw_items)} new history rows")

    if not raw_items:
        if reached_true_end:
            set_walk_complete(conn, "plex", USER_ID, True)
            log("Reached true end of Plex history — full walk marked complete")
        log("No new activity — nothing to do")
        if conn:
            conn.close()
        _emit_result(0, len(raw_items), full_pull)
        sys.exit(0)

    watched_by_key = parse_items(raw_items)
    log(f"{len(watched_by_key)} unique (series, season) combos touched since last sync")

    user_list, title_index = load_user_list_from_db()
    log(f"Loaded {len(user_list)} AniList entries, {len(title_index)} title variants indexed (from local mirror)")

    title_search_cache = load_title_search_cache(conn)
    seed_search_cache(title_search_cache)
    log(f"Loaded {len(title_search_cache)} cached AniList title-search results")

    updated = skipped = no_change = index_hits = search_hits = 0

    for (title, season), watched in sorted(watched_by_key.items()):
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

        decision = resolve_or_create_user_list_entry(
            media_id, title, user_list, full_pull, conn,
            watched_format=watched["watched_format"], watched_episode_count=watched["episode"],
        )
        if decision == "skip":
            log(f"  ✗ Not in your AniList (or an implausible/unvalidated match): '{title}'")
            skipped += 1
            continue
        if decision == "create":
            log(f"  + Not yet tracked — creating a new AniList entry: '{title}'")

        entry = dict(user_list[media_id])
        entry["anilist_id"] = media_id

        # Issue #387 — a "create" decision above already validated plausibility
        # against real AniList metadata inside resolve_or_create_user_list_entry();
        # this second check only ever matters for the "existing" decision (real
        # AniList data from the local mirror, not a synthetic placeholder) — kept
        # as a harmless, redundant safety net on the create path rather than
        # branching around it.
        if not is_plausible_match(entry, watched["watched_format"], watched["episode"]):
            log(f"  ✗ Implausible match, skipping: '{title}' "
                f"(AniList format={entry.get('format')}, total_eps={entry.get('total_episodes')}; "
                f"watched format={watched['watched_format']}, ep={watched['episode']})")
            skipped += 1
            continue

        plex_state = state_map.get(media_id)
        try:
            result = process(title, watched["episode"], entry, plex_state, conn)
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
        set_walk_complete(conn, "plex", USER_ID, True)
        log("Reached true end of Plex history — full walk marked complete")

    if conn:
        conn.close()
    log(f"Done — {updated} updated, {no_change} unchanged, {skipped} skipped/unmatched "
        f"({index_hits} index hits, {search_hits} API searches)"
        + (" [DRY RUN — nothing was written]" if DRY_RUN else ""))
    _emit_result(updated, len(raw_items), full_pull)


if __name__ == "__main__":
    main()
