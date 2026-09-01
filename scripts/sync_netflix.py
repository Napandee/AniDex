#!/usr/bin/env python3
"""
Netflix → AniList sync (cookie-replay, incremental, state-aware).

Fetches this user's Netflix viewing activity via Netflix's own "Falcor"
JSON-graph API (api/aui/pathEvaluator — the same one the real netflix.com/
viewingactivity page uses for its "show more" pagination), newest-first,
stopping once an item at/older than the stored watermark is reached. Diffs
against last-known state in Postgres, then updates AniList with the same
discipline sync_crunchyroll.py uses: never creates new AniList entries, never
goes backwards on progress, never touches score or notes.

CONFIRMED AGAINST A LIVE ACCOUNT on feature/netflix-sync-48 (2026-08-14) via
scripts/dev/probe_netflix_falcor.py — this replaced an earlier assumption
(the Shakti REST endpoint, api/shakti/.../viewingactivity) that consistently
returned HTTP 421 in live testing and turned out not to be what the current
web client actually calls. Full investigation notes on that branch.

Auth needs the FULL Netflix cookie jar (NETFLIX_COOKIE_HEADER) — not just
NetflixId/SecureNetflixId, which is what the (wrong) Shakti assumption
needed — plus the profile guid (NETFLIX_PROFILE_GUID), both extracted once
from a logged-in browser session the same way as before.

IMPORTANT LIMITATION, confirmed live: Netflix's Falcor feed has no absolute
episode-ordinal field — items carry `seriesTitle`/`seasonDescriptor`/
`episodeTitle`/`movieID`/`date`, never a numeric "episode 5" position. So
this can't compute an absolute progress number the way sync_crunchyroll.py
does from CR's max-aggregated history. Instead: this counts DISTINCT new
episodes per series (deduped by movieID) and adds that count to AniList's
own current progress — decided as the "date-order heuristic" for issue #48.
This is correct for the common case (watching forward, in order) but will
overcount if episodes are watched out of order or a whole season is skipped
and picked up later; same failure-mode ceiling already accepted elsewhere in
this app (a wrong progress number is a one-click manual fix, score/notes are
never touched).

"New" for that per-series count is decided per-series, not just by whether
fetch_since() returned the item at all: each series' items are filtered
against THAT series' own last_seen_watched_at (stored in netflix_sync_state),
not only the fetch's single global watermark (the max across all series,
used purely to know when pagination can stop — see compute_fetch_watermark()).
Under the normal incremental path the two coincide, so this is a no-op. It
matters once anything ever bypasses the global watermark (e.g. a future
Force Full Resync for Netflix) — a full re-fetch returns each series' ENTIRE
history, and without the per-series filter, "new" would mean the whole
series instead of the genuine incremental tail, inflating progress by
roughly the already-watched count on the very next run (#69). See
`_new_episode_count()`.

Movie items in this feed haven't been directly observed in live testing
(only TV episodes were in the test account's recent history) — the movie
handling below is inferred from the schema (no `series` key) rather than
confirmed. Flag if it turns out wrong on a real movie-watch.

Exit 0 = success, Exit 1 = fatal error. Matches the other scripts/sync_*.py
scripts' contract.
"""

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

from anilist_sync_common import (
    db_connect, determine_full_pull_watermark, emit_sync_result, enqueue_outbox_update,
    find_anilist_id, is_plausible_match, load_title_search_cache, load_user_list_from_db,
    make_logger, mark_walk_complete_if_reached_end, resolve_or_create_user_list_entry,
    save_title_search_cache_entry, seed_search_cache,
    set_walk_complete,  # noqa: F401 — re-exported for import_netflix_csv.py's nf.set_walk_complete() call
)

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])
NETFLIX_COOKIE_HEADER = os.environ.get("NETFLIX_COOKIE_HEADER", "")
NETFLIX_PROFILE_GUID = os.environ.get("NETFLIX_PROFILE_GUID", "")

# When set, logs every AniList update / state write this run *would* make instead of
# making it — useful whenever the fetch/parse logic changes, since a live run doubles
# as both a functional test and a real write to AniList.
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

# Issue #21 — set by run_full_sync.py's Force Full Resync path to bypass the stored
# watermark for one run. Mirrors sync_crunchyroll.py's FORCE_FULL_RESYNC handling; safe
# here because _new_episode_count() still filters against each series' own per-series
# watermark (nf_state_map[...]["last_seen_watched_at"]), which this flag doesn't touch —
# only the global fetch watermark passed to fetch_since() is bypassed (issue #19 fixed
# the progress math so that stays correct even on a full re-fetch).
FORCE_FULL_RESYNC = os.environ.get("FORCE_FULL_RESYNC", "").strip().lower() in ("1", "true", "yes")

PAGE_SIZE = 50
MAX_PAGES = 200  # safety cap — the fetch loop should always stop at the watermark
                  # well before this; this just prevents a runaway loop if Netflix's
                  # response shape doesn't match what's expected here.

# XHR-only headers — a real browser never sends these on a plain page navigation
# (only on the actual API call), confirmed live: sending them on the /browse GET
# used to resolve build_id gets a 400 Bad Request back from Netflix's edge.
BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}

# Most of these are static device-type identifiers (browser/OS/client-type, ESN
# prefix), not per-session secrets — confirmed live that a generic "Chrome on
# Linux" identity works regardless of the actual host running this script.
API_HEADERS = {
    "accept": "*/*",
    "content-type": "application/x-www-form-urlencoded",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "x-netflix.browsername": "Chrome",
    "x-netflix.browserversion": "151",
    "x-netflix.clienttype": "akira",
    "x-netflix.esnprefix": "NFCDCH-LX-",
    "x-netflix.nq.stack": "prod",
    "x-netflix.osfullname": "Linux",
    "x-netflix.osname": "Linux",
    "x-netflix.osversion": "0.0.0",
    "x-netflix.client.request.name": "ui/xhrUnclassified",
    "x-netflix.request.attempt": "1",
    "x-netflix.request.client.context": '{"appstate":"foreground"}',
    "x-netflix.request.routing": (
        '{"path":"/nq/aui/endpoint/%5E1.0.0-web/pathEvaluator","control_tag":"auinqweb"}'
    ),
}


log = make_logger("netflixsync", USER_ID)


def _emit_result(entries_updated: int, entries_fetched: int, full_pull: bool) -> None:
    """emit_sync_result(), routed through this script's own DRY_RUN — see
    anilist_sync_common.emit_sync_result()'s docstring (issue #46/#414)."""
    emit_sync_result(entries_updated, entries_fetched, full_pull, DRY_RUN)


def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
    cookies = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
    return cookies


# ── Netflix Falcor client ────────────────────────────────────────────────────

class NetflixHistory:
    """Cookie-authenticated Falcor pathEvaluator client for viewingActivity."""

    def __init__(self, cookie_header: str, profile_guid: str):
        self.profile_guid = profile_guid
        self.client = httpx.Client(
            cookies=_parse_cookie_header(cookie_header),
            headers=BROWSER_HEADERS,
            timeout=30,
            follow_redirects=True,
        )
        self._build_id: str | None = None

    def _resolve_build_id(self) -> str:
        """The API path includes a build_id that changes with Netflix's deploys, so it
        can't be stored as a static credential — resolve it fresh each run from a
        logged-in page's embedded state."""
        if self._build_id:
            return self._build_id
        resp = self.client.get("https://www.netflix.com/browse")
        resp.raise_for_status()
        match = re.search(r'"BUILD_IDENTIFIER"\s*:\s*"([^"]+)"', resp.text)
        if not match:
            raise RuntimeError(
                "Could not resolve Netflix build_id from page state — Netflix may have "
                "changed its page structure, or the session cookies are invalid/expired."
            )
        self._build_id = match.group(1)
        return self._build_id

    def _fetch_page(self, page: int) -> list[dict]:
        build_id = self._resolve_build_id()
        url = "https://www.netflix.com/api/aui/pathEvaluator/web/%5E2.0.0"
        params = {
            "method": "call",
            "callPath": json.dumps(["aui", "viewingActivity", page, PAGE_SIZE]),
            "falcor_server": "0.1.0",
        }
        body = {"param": json.dumps({"guid": self.profile_guid})}
        headers = {
            **API_HEADERS,
            "x-netflix.uiversion": build_id,
            "x-netflix.request.id": uuid.uuid4().hex,
            "referer": "https://www.netflix.com/viewingactivity",
            "origin": "https://www.netflix.com",
        }
        resp = self.client.post(url, params=params, data=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return (
            data.get("jsonGraph", {}).get("aui", {}).get("viewingActivity", {})
            .get("value", {}).get("viewedItems", [])
        )

    def fetch_since(self, watermark: datetime | None) -> tuple[list[dict], bool]:
        """Fetch viewing history newest-first, stopping once an item at/older than
        `watermark` is hit, or a short page signals the end of history. Returns
        (raw viewedItems dicts (unparsed), reached_true_end_of_history).

        reached_true_end is True only when pagination stopped because history
        genuinely ran out (a short or empty page) — not because it hit the given
        watermark or the MAX_PAGES safety cap. Issue #97 needs this distinction
        to know when a full walk has actually finished vs. just caught up to a
        previously-known point."""
        items: list[dict] = []
        reached_true_end = False

        for page in range(1, MAX_PAGES + 1):
            page_items = self._fetch_page(page)
            if not page_items:
                reached_true_end = True
                break

            reached_watermark = False
            for item in page_items:
                watched_at = _item_watched_at(item)
                if watermark and watched_at and watched_at <= watermark:
                    reached_watermark = True
                    break
                items.append(item)

            if len(page_items) < PAGE_SIZE:
                reached_true_end = True
                break
            if reached_watermark:
                break
        else:
            log(f"WARNING: hit the {MAX_PAGES}-page safety cap without reaching the "
                f"watermark — response shape may not match what this script expects.")

        return items, reached_true_end


def _item_watched_at(item: dict) -> datetime | None:
    """Falcor's `date` field is epoch milliseconds — confirmed live."""
    ms = item.get("date")
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _is_episode(item: dict) -> bool:
    """TV episodes carry a `series` (numeric show id) — confirmed live. Movie
    item shape is inferred (no `series` key), not directly observed."""
    return bool(item.get("series"))


def aggregate_by_series(items: list[dict]) -> dict[str, dict]:
    """Returns {title: {items: [(movie_id, watched_at), ...], watched_format}}.

    Deliberately keeps each item's own (movie_id, watched_at) pair rather than
    pre-reducing to a single new_count here: matching a Netflix title to a
    specific AniList entry — and that entry's own stored last_seen_watched_at
    — only happens in main(), and _new_episode_count() needs per-item
    timestamps to filter against that series-specific watermark rather than
    just the fetch's single global one (see module docstring, #69).
    """
    by_series: dict[str, dict] = {}
    for item in items:
        is_ep = _is_episode(item)
        title = ((item.get("seriesTitle") if is_ep else item.get("title")) or "").strip()
        if not title:
            continue
        movie_id = item.get("movieID")
        watched_at = _item_watched_at(item) or datetime.min.replace(tzinfo=timezone.utc)

        entry = by_series.setdefault(title, {
            "items": [],
            "watched_format": "TV" if is_ep else "MOVIE",
        })
        entry["items"].append((movie_id, watched_at))

    return by_series


def _new_episode_count(
    items: list[tuple], watermark: datetime | None
) -> tuple[int, datetime | None]:
    """Reduces a series' raw (movie_id, watched_at) pairs to (new_count, watched_at),
    counting only items genuinely newer than `watermark` — that series' own
    last_seen_watched_at, not just the fetch's single global watermark. Deduped by
    movie_id (Netflix's stable per-episode content id, so re-pagination or a
    rewatch-within-this-fetch can't double-count the same episode), matching the
    prior behavior for a normal incremental fetch exactly.

    This is what keeps new_count safe under a forced full re-fetch: without this
    per-series filter, a full re-fetch (global watermark=None) would return each
    series' entire history, and "new" would silently mean "everything Netflix has
    ever recorded for this show" instead of the genuine incremental tail.

    Returns (0, None) if nothing survives the filter — the caller must skip the
    series entirely in that case, not just treat it as zero new episodes. Some of
    process()'s branches (e.g. rewatch-start) key off "any new activity present"
    rather than new_count alone, and a series can appear in `items` at all under a
    full re-fetch purely because of OTHER series' more recent activity, not
    because anything new happened for this one.
    """
    relevant = [(mid, wat) for mid, wat in items if not watermark or wat > watermark]
    if not relevant:
        return 0, None
    movie_ids = {mid for mid, _ in relevant if mid is not None}
    return len(movie_ids) or 1, max(wat for _, wat in relevant)


# ── Postgres ──────────────────────────────────────────────────────────────────

def ensure_table(conn):
    """Defensive fallback if run against a DB that somehow skipped schema.sql/migrations
    — see sync_crunchyroll.py's ensure_table() for the same rationale."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS netflix_sync_state (
                user_id                INTEGER NOT NULL,
                anilist_id             INTEGER NOT NULL,
                series_title           TEXT,
                last_seen_watched_at   TIMESTAMPTZ,
                rewatch_in_progress    BOOLEAN NOT NULL DEFAULT FALSE,
                last_synced_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, anilist_id)
            )
        """)
    conn.commit()


def load_nf_state(conn) -> dict[int, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT anilist_id, last_seen_watched_at, rewatch_in_progress "
            "FROM netflix_sync_state WHERE user_id = %s",
            (USER_ID,),
        )
        return {row["anilist_id"]: dict(row) for row in cur.fetchall()}


def save_nf_state(conn, anilist_id: int, title: str, watched_at: datetime, rewatch: bool):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO netflix_sync_state (user_id, anilist_id, series_title, last_seen_watched_at, rewatch_in_progress, last_synced_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (user_id, anilist_id) DO UPDATE SET
                series_title          = EXCLUDED.series_title,
                last_seen_watched_at  = EXCLUDED.last_seen_watched_at,
                rewatch_in_progress   = EXCLUDED.rewatch_in_progress,
                last_synced_at        = now()
        """, (USER_ID, anilist_id, title, watched_at, rewatch))
    conn.commit()




# ── Sync logic ────────────────────────────────────────────────────────────────

def _update(conn, anilist_id: int, **kwargs):
    """enqueue_outbox_update(), routed through DRY_RUN — see its module-level
    docstring. Issue #100 — no longer pushes to AniList directly/synchronously;
    enqueues to status_sync_outbox for the app's shared outbox worker to deliver."""
    if DRY_RUN:
        log(f"    [dry-run] would enqueue outbox update({anilist_id}, {kwargs})")
    else:
        enqueue_outbox_update(conn, anilist_id, "netflix", **kwargs)


def _save_state(conn, anilist_id: int, title: str, watched_at: datetime, rewatch: bool):
    """save_nf_state(), routed through DRY_RUN — see its module-level docstring."""
    if DRY_RUN:
        log(f"    [dry-run] would save state: anilist_id={anilist_id} "
            f"watched_at={watched_at} rewatch={rewatch}")
    else:
        save_nf_state(conn, anilist_id, title, watched_at, rewatch)


def process(title: str, watched: dict, entry: dict, nf_state: dict | None, conn) -> str:
    """
    Apply update logic for one series/movie. Returns a short description of the
    action taken. nf_state may be None on first sighting of this series.

    watched["episode"] is AniList's current progress + however many distinct new
    episodes this sync saw for the series (see aggregate_by_series/main) — an
    absolute position for the "continuing" cases, but NOT usable to detect "a
    rewatch started from episode 1" the way sync_crunchyroll.py's max-aggregated
    CR history can (it can never look lower than al_ep by construction here).
    watched["new_count"] is used instead for that one case — see below.
    """
    status = entry["status"]
    al_ep = entry["progress"]
    repeat = entry["repeat"]
    total = entry["total_episodes"]
    anilist_id = entry["anilist_id"]
    watched_ep = watched["episode"]
    watched_at = watched["watched_at"]
    rewatch_active = nf_state["rewatch_in_progress"] if nf_state else False

    # ── Movies: a single watch event ──────────────────────────────────────────
    # Checked before the brand-new-entry branch below (issue #252, corrected in
    # review): a movie is watched in one sitting, so "still watching" is never
    # the right resting state for it — including for a freshly-created entry.
    # A synthetic new entry always has al_ep=0 and status=None, which satisfies
    # neither of the first two conditions here, so it falls straight into the
    # `al_ep < 1` case below and lands COMPLETED at progress=1 — exactly the
    # same first-watch handling an existing PLANNING movie entry already gets.
    # No special-casing needed; this is the same code path, not a new one.
    if watched["watched_format"] == "MOVIE":
        if status == "COMPLETED" and al_ep >= 1:
            _update(conn, anilist_id, repeat=repeat + 1)
            _save_state(conn, anilist_id, title, watched_at, False)
            return f"movie rewatch → repeat #{repeat + 1}"
        if al_ep < 1:
            _update(conn, anilist_id, progress=1, status="COMPLETED")
            _save_state(conn, anilist_id, title, watched_at, False)
            return "movie watched → COMPLETED"
        _save_state(conn, anilist_id, title, watched_at, rewatch_active)
        return "movie — already COMPLETED, no change"

    # ── Issue #252: brand-new AniList entry, not yet on the user's list ──────
    # main() only ever builds a synthetic entry (for an incremental sync's
    # unmatched-title case) with status=None — a real AniList entry's status is
    # never None, so this is an unambiguous "create" sentinel. Only reached for
    # non-movie (TV/series) formats — a new movie entry is already handled
    # correctly by the MOVIE branch above (COMPLETED, not WATCHING — there's no
    # sensible "still watching" state for single-sitting content).
    if status is None:
        _update(conn, anilist_id, progress=watched_ep, status="WATCHING")
        _save_state(conn, anilist_id, title, watched_at, False)
        return f"new AniList entry created → WATCHING ep {watched_ep}"

    # ── First sighting of a COMPLETED series — can't safely tell rewatch from
    # first sync without a baseline. Record and wait for next sync.
    if nf_state is None and status == "COMPLETED":
        _save_state(conn, anilist_id, title, watched_at, False)
        return "first-sync (COMPLETED) — state recorded, no change"

    # ── AniList status already REPEATING but rewatch not recorded in state ────
    # Issue #100 — _update() now enqueues to the outbox rather than pushing to
    # AniList directly, and doesn't commit; call order with _save_state() (which
    # does commit) no longer matters for correctness the way it used to when
    # _update() was a network call that could fail mid-flight — both now land in
    # one atomic transaction, so either the outbox row + advanced watermark both
    # persist or neither does. Kept in this order for consistency/minimal diff.
    if status == "REPEATING" and not rewatch_active:
        if watched_ep > al_ep:
            _update(conn, anilist_id, progress=watched_ep)
            _save_state(conn, anilist_id, title, watched_at, True)
            return f"rewatch detected (already REPEATING) → progress {al_ep} → {watched_ep}"
        _save_state(conn, anilist_id, title, watched_at, True)
        return "rewatch detected (already REPEATING) — state recorded"

    # ── Rewatch starting: any new activity on an already-COMPLETED series is
    # itself the signal (fetch_since() only returns genuinely new activity, and
    # nf_state existing here means this isn't the first-ever sighting handled
    # above) — start counting fresh from new_count, not al_ep + new_count, since
    # a rewatch begins at episode 1 again.
    if status == "COMPLETED" and not rewatch_active:
        fresh_progress = watched["new_count"]
        _update(conn, anilist_id, progress=fresh_progress, status="REPEATING")
        _save_state(conn, anilist_id, title, watched_at, True)
        return f"rewatch started → REPEATING ep {fresh_progress}"

    # ── Rewatch completion ─────────────────────────────────────────────────────
    if rewatch_active and total and watched_ep >= total:
        _update(conn, anilist_id, progress=watched_ep, status="COMPLETED", repeat=repeat + 1)
        _save_state(conn, anilist_id, title, watched_at, False)
        return f"rewatch complete → COMPLETED (repeat #{repeat + 1})"

    # ── Progress advance for active rewatch ───────────────────────────────────
    if rewatch_active and watched_ep > al_ep:
        _update(conn, anilist_id, progress=watched_ep)
        _save_state(conn, anilist_id, title, watched_at, True)
        return f"rewatch progress {al_ep} → {watched_ep}"

    # ── DROPPED: user picked it back up ──────────────────────────────────────
    if status == "DROPPED" and watched_ep > al_ep:
        _update(conn, anilist_id, progress=watched_ep, status="CURRENT")
        _save_state(conn, anilist_id, title, watched_at, False)
        return f"resumed after DROP → CURRENT ep {watched_ep}"

    # ── Normal progress advance (CURRENT, PAUSED) ─────────────────────────────
    if watched_ep > al_ep:
        _update(conn, anilist_id, progress=watched_ep)
        _save_state(conn, anilist_id, title, watched_at, False)
        return f"progress {al_ep} → {watched_ep}"

    # Nothing to do
    _save_state(conn, anilist_id, title, watched_at, rewatch_active)
    return f"AniList ({al_ep}) already at or ahead of Netflix ({watched_ep})"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Starting Netflix → AniList sync")

    if not NETFLIX_COOKIE_HEADER or not NETFLIX_PROFILE_GUID:
        log("ERROR: Netflix credentials not configured (NETFLIX_COOKIE_HEADER / NETFLIX_PROFILE_GUID)")
        sys.exit(1)

    if DRY_RUN:
        # No DB touched at all in dry-run — not even the CREATE TABLE IF NOT EXISTS
        # fallback, since that would create netflix_sync_state ahead of the reviewed
        # migration actually being run. Treated as a from-scratch first sync (no
        # watermark), which is also the most useful dry-run shape: it exercises the
        # fetch/parse/match/process path against your full real history.
        log("[dry-run] skipping database entirely — no reads, no writes")
        conn = None
        nf_state_map: dict[int, dict] = {}
    else:
        conn = db_connect()
        ensure_table(conn)
        nf_state_map = load_nf_state(conn)
        log(f"Loaded Netflix sync state for {len(nf_state_map)} series")

    # Issue #252 — a full-pull run (initial connect OR a user-triggered Force Full
    # Resync, #21 — both land here via watermark being None) keeps the conservative
    # skip-if-untracked behavior below; only a genuine day-to-day incremental sync
    # (watermark is not None) is allowed to auto-create a new AniList entry.
    watermark, full_pull = determine_full_pull_watermark(
        conn, "netflix", USER_ID, nf_state_map, FORCE_FULL_RESYNC, log,
    )
    log(f"Fetching Netflix viewing activity since {watermark or '(no watermark — full walk)'}")

    client = NetflixHistory(NETFLIX_COOKIE_HEADER, NETFLIX_PROFILE_GUID)
    try:
        raw_items, reached_true_end = client.fetch_since(watermark)
    except Exception as e:
        log(f"ERROR: Netflix fetch failed: {e}")
        if conn:
            conn.close()
        sys.exit(1)
    log(f"Fetched {len(raw_items)} new viewing-activity rows")

    if not raw_items:
        # Issue #104 — safe to mark complete here even though the processing loop
        # below never ran: there's nothing to process, so nothing can be stranded.
        mark_walk_complete_if_reached_end(conn, "netflix", USER_ID, reached_true_end, "Netflix", log)
        log("No new activity — nothing to do")
        if conn:
            conn.close()
        _emit_result(0, len(raw_items), full_pull)
        sys.exit(0)

    watched_by_series = aggregate_by_series(raw_items)
    log(f"{len(watched_by_series)} unique series/movies touched since last sync")

    # Issue #99 — reads the local library_entries mirror instead of making our own
    # live AniList API call. run_full_sync.py now runs anilist_postgres before this
    # step specifically so the mirror is fresh.
    user_list, title_index = load_user_list_from_db()
    log(f"Loaded {len(user_list)} AniList entries, {len(title_index)} title variants indexed (from local mirror)")

    # Issue #115 — seed find_anilist_id()'s search fallback from what's already been
    # resolved by any previous sync (any user, any provider), so this run doesn't
    # re-search titles already known to match or known to have no match.
    title_search_cache = load_title_search_cache(conn)
    seed_search_cache(title_search_cache)
    log(f"Loaded {len(title_search_cache)} cached AniList title-search results")

    updated = skipped = no_change = index_hits = search_hits = 0

    for title, agg in sorted(watched_by_series.items()):
        normalized = title.lower()
        in_index_before = normalized in title_index
        media_id = find_anilist_id(title, title_index)
        if not in_index_before and title not in title_search_cache:
            # A genuinely new search result this run — persist immediately, not just
            # at the end, so an interrupted run doesn't lose it (same durability
            # principle as #104's walk_complete fix).
            save_title_search_cache_entry(conn, title, media_id)
            title_search_cache[title] = media_id
        if in_index_before and media_id:
            index_hits += 1
        elif media_id:
            search_hits += 1
        if not media_id:
            log(f"  ✗ No AniList match: '{title}'")
            skipped += 1
            continue

        # Issue #387 — moved ahead of resolve_or_create_user_list_entry(): that
        # call now needs a real watched_episode_count to validate a create
        # decision, and Netflix's own formula for what that count WOULD be
        # (see watched_ep below) only depends on new_count/agg — never on
        # user_list/the decision itself — so computing it first is safe and
        # doesn't change anything about the "existing" path below, which still
        # recomputes watched_ep from the real entry["progress"] exactly as
        # before. nf_state_map lookups are Netflix's own state, independent of
        # AniList-tracking status either way.
        nf_state = nf_state_map.get(media_id)
        per_series_watermark = nf_state.get("last_seen_watched_at") if nf_state else None
        new_count, watched_at = _new_episode_count(agg["items"], per_series_watermark)
        if watched_at is None:
            # Nothing in this fetch is actually newer than THIS series' own
            # last-seen point — it only showed up here because some other
            # series' more recent activity kept fetch_since() going past it
            # (or, under a future full re-fetch, because the whole history
            # came back). Not genuine new activity for this series — skip.
            continue

        # Issue #252 — incremental sync: genuinely new watch activity for a title
        # that isn't tracked yet should originate a new AniList entry instead of
        # being silently dropped forever. Full-pull runs (initial connect or a
        # Force Full Resync, #21) keep the original skip behavior. conn may be
        # None here in DRY_RUN — resolve_or_create_user_list_entry() skips the DB
        # write in that case but still exercises the same decision. See its
        # docstring for the full contract. watched_episode_count here is what
        # watched_ep would be for a brand-new entry specifically (progress
        # starts at 0 for a synthetic placeholder, so 0 + new_count == new_count)
        # — issue #387's create-path plausibility gate only ever consults this
        # value when the decision is actually "create", so it doesn't need to
        # match the "existing" path's own (different) watched_ep formula below.
        decision = resolve_or_create_user_list_entry(
            media_id, title, user_list, full_pull, conn,
            watched_format=agg["watched_format"],
            watched_episode_count=(1 if agg["watched_format"] == "MOVIE" else new_count),
        )
        if decision == "skip":
            log(f"  ✗ Not in your AniList (or an implausible/unvalidated match): '{title}'")
            skipped += 1
            continue
        if decision == "create":
            log(f"  + Not yet tracked — creating a new AniList entry: '{title}'")

        entry = dict(user_list[media_id])
        entry["anilist_id"] = media_id

        # Turn "N new episodes seen" into an absolute AniList progress number —
        # see module docstring / process()'s docstring for why this can't be an
        # absolute position parsed directly from Netflix's data.
        watched_ep = 1 if agg["watched_format"] == "MOVIE" else entry["progress"] + new_count
        watched = {
            "watched_format": agg["watched_format"],
            "watched_at": watched_at,
            "new_count": new_count,
            "episode": watched_ep,
        }

        # Issue #387 — a "create" decision above already validated plausibility
        # against real AniList metadata inside resolve_or_create_user_list_entry();
        # this second check only ever matters for the "existing" decision (real
        # AniList data from the local mirror, not a synthetic placeholder) — kept
        # as a harmless, redundant safety net on the create path rather than
        # branching around it.
        if not is_plausible_match(entry, watched["watched_format"], watched_ep or None):
            log(f"  ✗ Implausible match, skipping: '{title}' "
                f"(AniList format={entry.get('format')}, total_eps={entry.get('total_episodes')}; "
                f"watched format={watched['watched_format']}, ep={watched_ep})")
            skipped += 1
            continue

        try:
            result = process(title, watched, entry, nf_state, conn)
            log(f"  '{title}': {result}")
            if "→" in result:
                updated += 1
            else:
                no_change += 1
        except Exception as e:
            log(f"  ERROR processing '{title}': {e}")
            skipped += 1

    # Issue #104 — only mark the walk complete once every fetched title has actually
    # been processed. Marking it right after fetch (the original #97 shape) risked
    # permanently stranding matches if this loop got interrupted partway through: a
    # later sync would trust walk_complete and never re-walk to pick up what was
    # fetched but never reached.
    mark_walk_complete_if_reached_end(conn, "netflix", USER_ID, reached_true_end, "Netflix", log)

    if conn:
        conn.close()
    log(f"Done — {updated} updated, {no_change} unchanged, {skipped} skipped/unmatched "
        f"({index_hits} index hits, {search_hits} API searches)"
        + (" [DRY RUN — nothing was written]" if DRY_RUN else ""))
    _emit_result(updated, len(raw_items), full_pull)


if __name__ == "__main__":
    main()
