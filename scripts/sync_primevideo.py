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
from dotenv import load_dotenv

from anilist_sync_common import (
    classify_fetch_error,
    compute_fetch_watermark as _compute_fetch_watermark,
    db_connect, determine_full_pull_watermark, emit_sync_error, emit_sync_result,
    enqueue_outbox_update, find_anilist_id, is_plausible_match,
    load_title_search_cache, load_user_list_from_db, make_logger,
    mark_walk_complete_if_reached_end, process_max_aggregated_progress,
    resolve_or_create_user_list_entry, save_provider_state, save_provider_watermark,
    save_title_search_cache_entry, season_suffix_candidates, seed_search_cache,
)

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])
PRIMEVIDEO_COOKIE_HEADER = os.environ.get("PRIMEVIDEO_COOKIE_HEADER", "")

# Issue #387, Part 2 — same DRY_RUN pattern sync_netflix.py already had (this script
# had none until now, which is exactly why today's #352 debugging had no safe way to
# exercise a real fetch against the live account without risking real DB writes — see
# the walk-complete fix's docstring in anilist_sync_common.py for how that played out).
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

# Issue #21's pattern (Force Full Resync), wired in from day one per this issue's own
# scope — #20/#21's precedent already exists, no reason to defer this to a follow-up
# the way Plex's did (see sync_plex.py's comment on the same env var).
FORCE_FULL_RESYNC = os.environ.get("FORCE_FULL_RESYNC", "").strip().lower() in ("1", "true", "yes")

MAX_PAGES = 200  # safety cap — matches sync_crunchyroll.py/sync_netflix.py/sync_plex.py

_BASE_URL = "https://www.primevideo.com/api/getWatchHistorySettingsPage"

_SEASON_RE = re.compile(r"^(.*?)[\s,\-]+Season\s*0*(\d+)\b", re.IGNORECASE)
_BARE_SEASON_RE = re.compile(r"^Season\s*0*\d+$", re.IGNORECASE)
_EPISODE_RE = re.compile(r"^Episode\s+(\d+)\b", re.IGNORECASE)


log = make_logger("primevideosync", USER_ID)


def _emit_result(entries_updated: int, entries_fetched: int, full_pull: bool) -> None:
    """emit_sync_result(), routed through this script's own DRY_RUN — see
    anilist_sync_common.emit_sync_result()'s docstring (issue #46/#414)."""
    emit_sync_result(entries_updated, entries_fetched, full_pull, DRY_RUN)


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
                # Chromium Client Hints + Fetch Metadata headers — a real browser
                # sends these on every XHR/fetch automatically. Confirmed the hard
                # way (issue #352): this class originally omitted all six, and the
                # live sync started 403ing shortly after scripts/dev/probe_primevideo_history.py
                # (which does send them) succeeded with the exact same cookie —
                # not cookie expiry, a WAF check on these specific headers.
                "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Linux"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
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
    found at all, and to ("", 1) — genuinely unusable, skipped by
    parse_items()'s `if not title: continue` guard — for the bare "Season 3"
    case (no show name attached at all).

    Issue #387: that bare-"Season N" case used to fall through to the same
    (display_title, 1) fallback as any other unparseable title, returning the
    literal string "Season 3" as if it were a real series name. The intent
    (per this docstring, unchanged since #17) was always that a title with
    nothing usable "simply won't resolve against AniList and gets skipped" —
    but AniList's search is fuzzy, not exact, and a live incident confirmed
    the literal query "Season 3" actually found "Dorohedoro Season 3" (a real,
    unrelated anime) as a false-positive hit, auto-created as a new AniList
    entry. _BARE_SEASON_RE now catches this shape explicitly and returns an
    empty title so it's skipped upstream of ever reaching AniList's search at
    all, rather than relying on the search finding nothing."""
    text = (display_title or "").strip()
    if not text:
        return text, 1
    if _BARE_SEASON_RE.match(text):
        return "", 1
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
    """save_provider_state() bound to primevideo_sync_state (issue #414) — kept as
    its own function (rather than a bare re-export) so tests can still monkeypatch
    sync_primevideo.save_pv_state directly, same as before this consolidation."""
    save_provider_state(conn, "primevideo_sync_state", USER_ID, anilist_id, title, last_ep, rewatch)


def save_watermark(conn, anilist_id: int, title: str, watched_at: datetime):
    """save_provider_watermark() bound to primevideo_sync_state (issue #414) — see
    its docstring for why this stays column-disjoint from save_pv_state()."""
    save_provider_watermark(conn, "primevideo_sync_state", USER_ID, anilist_id, title, watched_at)


def compute_fetch_watermark(state_map: dict[int, dict]) -> datetime | None:
    """Re-exported under this script's own name (issue #414) — see
    sync_crunchyroll.py's identical wrapper for why."""
    return _compute_fetch_watermark(state_map)


# ── Sync logic ────────────────────────────────────────────────────────────────
# process() mirrors sync_plex.py's process() (itself mirroring sync_crunchyroll.py's,
# including the #328 rewatch-clamp fix) verbatim — Prime Video's real absolute episode
# numbers put it in the same "max-aggregated history" shape as Crunchyroll/Plex, not
# Netflix's delta-count shape. Not re-derived here to avoid the three drifting.

def _update(conn, anilist_id: int, **kwargs):
    """enqueue_outbox_update(), routed through DRY_RUN — see its module-level
    docstring. Issue #100 — no longer pushes to AniList directly/synchronously;
    enqueues to status_sync_outbox for the app's shared outbox worker to
    deliver."""
    if DRY_RUN:
        log(f"    [dry-run] would enqueue outbox update({anilist_id}, {kwargs})")
    else:
        enqueue_outbox_update(conn, anilist_id, "primevideo", **kwargs)


def _save_state(conn, anilist_id: int, title: str, last_ep: int, rewatch: bool):
    """save_pv_state(), routed through DRY_RUN for a consistent log line — see
    _update()'s docstring. save_pv_state() itself already guards conn is None
    (safe to call directly), this wrapper exists purely so process()'s DRY_RUN
    output says what it would have saved, matching sync_netflix.py's pattern."""
    if DRY_RUN:
        log(f"    [dry-run] would save state: anilist_id={anilist_id} last_ep={last_ep} rewatch={rewatch}")
    else:
        save_pv_state(conn, anilist_id, title, last_ep, rewatch)


def process(title: str, pv_ep: int, entry: dict, pv_state: dict | None, conn) -> str:
    """The actual branching logic (state machine) is shared with
    sync_crunchyroll.py/sync_plex.py via
    anilist_sync_common.process_max_aggregated_progress() (issue #414) — this
    wrapper just binds it to Prime Video's own _update()/_save_state() (so
    DRY_RUN and test monkeypatching of those still work exactly as before) and
    label ("Prime Video")."""
    anilist_id = entry["anilist_id"]

    def update_fn(**kwargs):
        _update(conn, anilist_id, **kwargs)

    def save_state_fn(last_ep, rewatch):
        _save_state(conn, anilist_id, title, last_ep, rewatch)

    return process_max_aggregated_progress(
        title, pv_ep, entry, pv_state, "Prime Video", update_fn, save_state_fn,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Starting Prime Video → AniList sync")

    if not PRIMEVIDEO_COOKIE_HEADER:
        log("ERROR: Prime Video credentials not configured (PRIMEVIDEO_COOKIE_HEADER)")
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
        state_map = load_pv_state(conn)
        log(f"Loaded Prime Video sync state for {len(state_map)} series")

    watermark, full_pull = determine_full_pull_watermark(
        conn, "primevideo", USER_ID, state_map, FORCE_FULL_RESYNC, log,
    )
    log(f"Fetching Prime Video watch history since {watermark or '(no watermark — full walk)'}")

    client = PrimeVideoHistory(PRIMEVIDEO_COOKIE_HEADER)
    try:
        raw_items, reached_true_end = client.fetch_since(watermark)
    except Exception as e:
        log(f"ERROR: Prime Video fetch failed: {e}")
        error_type = classify_fetch_error(e)
        if error_type:
            log(f"  classified as: {error_type}")
            emit_sync_error(error_type)
        if conn:
            conn.close()
        sys.exit(1)
    log(f"Fetched {len(raw_items)} new history rows")

    if not raw_items:
        mark_walk_complete_if_reached_end(conn, "primevideo", USER_ID, reached_true_end, "Prime Video", log)
        log("No new activity — nothing to do")
        if conn:
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
        # this second check only ever matters for the "existing" decision (an
        # already-tracked entry, whose format/total_episodes are real AniList
        # data pulled from the local mirror, not a synthetic placeholder) — kept
        # as a harmless, redundant safety net on the create path rather than
        # branching around it.
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

    mark_walk_complete_if_reached_end(conn, "primevideo", USER_ID, reached_true_end, "Prime Video", log)

    if conn:
        conn.close()
    log(f"Done — {updated} updated, {no_change} unchanged, {skipped} skipped/unmatched "
        f"({index_hits} index hits, {search_hits} API searches)"
        + (" [DRY RUN — nothing was written]" if DRY_RUN else ""))
    _emit_result(updated, len(raw_items), full_pull)


if __name__ == "__main__":
    main()
