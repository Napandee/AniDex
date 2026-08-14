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
does from CR's max-aggregated history. Instead: fetch_since() already
returns only genuinely new items (newer than the stored watermark), so this
counts DISTINCT new episodes per series (deduped by movieID) and adds that
count to AniList's own current progress — decided as the "date-order
heuristic" for issue #48. This is correct for the common case (watching
forward, in order) but will overcount if episodes are watched out of order
or a whole season is skipped and picked up later; same failure-mode ceiling
already accepted elsewhere in this app (a wrong progress number is a
one-click manual fix, score/notes are never touched).

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
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from anilist_sync_common import anilist_update, fetch_user_list, find_anilist_id, is_plausible_match

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])
NETFLIX_COOKIE_HEADER = os.environ.get("NETFLIX_COOKIE_HEADER", "")
NETFLIX_PROFILE_GUID = os.environ.get("NETFLIX_PROFILE_GUID", "")

# When set, logs every AniList update / state write this run *would* make instead of
# making it — useful whenever the fetch/parse logic changes, since a live run doubles
# as both a functional test and a real write to AniList.
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

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


def log(msg):
    print(f"[netflixsync] user={USER_ID} {msg}", flush=True)


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

    def fetch_since(self, watermark: datetime | None) -> list[dict]:
        """Fetch viewing history newest-first, stopping once an item at/older than
        `watermark` is hit, or a short page signals the end of history. Returns raw
        viewedItems dicts (unparsed)."""
        items: list[dict] = []

        for page in range(1, MAX_PAGES + 1):
            page_items = self._fetch_page(page)
            if not page_items:
                break

            reached_watermark = False
            for item in page_items:
                watched_at = _item_watched_at(item)
                if watermark and watched_at and watched_at <= watermark:
                    reached_watermark = True
                    break
                items.append(item)

            if reached_watermark or len(page_items) < PAGE_SIZE:
                break
        else:
            log(f"WARNING: hit the {MAX_PAGES}-page safety cap without reaching the "
                f"watermark — response shape may not match what this script expects.")

        return items


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
    """Returns {title: {watched_at, new_count, watched_format}}.

    Netflix's Falcor feed has no absolute episode ordinal (see module
    docstring) — this counts DISTINCT new episodes per series, deduped by
    `movieID` (Netflix's stable per-episode content id, so re-pagination or a
    rewatch-within-this-fetch can't double-count the same episode). main()
    turns that count into an absolute AniList progress number.
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
            "watched_at": datetime.min.replace(tzinfo=timezone.utc),
            "movie_ids": set(),
            "watched_format": "TV" if is_ep else "MOVIE",
        })
        if movie_id is not None:
            entry["movie_ids"].add(movie_id)
        if watched_at > entry["watched_at"]:
            entry["watched_at"] = watched_at

    return {
        title: {
            "watched_at": data["watched_at"],
            "new_count": len(data["movie_ids"]) or 1,  # at least 1 — a title with no
                                                          # movieID still represents one
                                                          # watch event
            "watched_format": data["watched_format"],
        }
        for title, data in by_series.items()
    }


# ── Postgres ──────────────────────────────────────────────────────────────────

def db_connect():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


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


def compute_fetch_watermark(state_map: dict[int, dict]) -> datetime | None:
    """The single cursor fetch_since() paginates against — the newest
    last_seen_watched_at across all series from the previous sync. Netflix's
    viewingactivity feed is one chronological stream across all titles, so one
    watermark is enough to know when pagination has caught up, even though state
    is still tracked per-series (to drive per-series rewatch detection)."""
    values = [s["last_seen_watched_at"] for s in state_map.values() if s.get("last_seen_watched_at")]
    return max(values) if values else None


# ── Sync logic ────────────────────────────────────────────────────────────────

def _update(anilist_id: int, **kwargs):
    """anilist_update(), routed through DRY_RUN — see its module-level docstring."""
    if DRY_RUN:
        log(f"    [dry-run] would call anilist_update({anilist_id}, {kwargs})")
    else:
        anilist_update(anilist_id, **kwargs)


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
    if watched["watched_format"] == "MOVIE":
        if status == "COMPLETED" and al_ep >= 1:
            _update(anilist_id, repeat=repeat + 1)
            _save_state(conn, anilist_id, title, watched_at, False)
            return f"movie rewatch → repeat #{repeat + 1}"
        if al_ep < 1:
            _update(anilist_id, progress=1, status="COMPLETED")
            _save_state(conn, anilist_id, title, watched_at, False)
            return "movie watched → COMPLETED"
        _save_state(conn, anilist_id, title, watched_at, rewatch_active)
        return "movie — already COMPLETED, no change"

    # ── First sighting of a COMPLETED series — can't safely tell rewatch from
    # first sync without a baseline. Record and wait for next sync.
    if nf_state is None and status == "COMPLETED":
        _save_state(conn, anilist_id, title, watched_at, False)
        return "first-sync (COMPLETED) — state recorded, no change"

    # ── AniList status already REPEATING but rewatch not recorded in state ────
    if status == "REPEATING" and not rewatch_active:
        _save_state(conn, anilist_id, title, watched_at, True)
        if watched_ep > al_ep:
            _update(anilist_id, progress=watched_ep)
            return f"rewatch detected (already REPEATING) → progress {al_ep} → {watched_ep}"
        return "rewatch detected (already REPEATING) — state recorded"

    # ── Rewatch starting: any new activity on an already-COMPLETED series is
    # itself the signal (fetch_since() only returns genuinely new activity, and
    # nf_state existing here means this isn't the first-ever sighting handled
    # above) — start counting fresh from new_count, not al_ep + new_count, since
    # a rewatch begins at episode 1 again.
    if status == "COMPLETED" and not rewatch_active:
        fresh_progress = watched["new_count"]
        _update(anilist_id, progress=fresh_progress, status="REPEATING")
        _save_state(conn, anilist_id, title, watched_at, True)
        return f"rewatch started → REPEATING ep {fresh_progress}"

    # ── Rewatch completion ─────────────────────────────────────────────────────
    if rewatch_active and total and watched_ep >= total:
        _update(anilist_id, progress=watched_ep, status="COMPLETED", repeat=repeat + 1)
        _save_state(conn, anilist_id, title, watched_at, False)
        return f"rewatch complete → COMPLETED (repeat #{repeat + 1})"

    # ── Progress advance for active rewatch ───────────────────────────────────
    if rewatch_active and watched_ep > al_ep:
        _update(anilist_id, progress=watched_ep)
        _save_state(conn, anilist_id, title, watched_at, True)
        return f"rewatch progress {al_ep} → {watched_ep}"

    # ── DROPPED: user picked it back up ──────────────────────────────────────
    if status == "DROPPED" and watched_ep > al_ep:
        _update(anilist_id, progress=watched_ep, status="CURRENT")
        _save_state(conn, anilist_id, title, watched_at, False)
        return f"resumed after DROP → CURRENT ep {watched_ep}"

    # ── Normal progress advance (CURRENT, PAUSED) ─────────────────────────────
    if watched_ep > al_ep:
        _update(anilist_id, progress=watched_ep)
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

    watermark = compute_fetch_watermark(nf_state_map)
    log(f"Fetching Netflix viewing activity since {watermark or '(no watermark — first sync, full pull)'}")

    client = NetflixHistory(NETFLIX_COOKIE_HEADER, NETFLIX_PROFILE_GUID)
    try:
        raw_items = client.fetch_since(watermark)
    except Exception as e:
        log(f"ERROR: Netflix fetch failed: {e}")
        if conn:
            conn.close()
        sys.exit(1)
    log(f"Fetched {len(raw_items)} new viewing-activity rows")

    if not raw_items:
        log("No new activity — nothing to do")
        if conn:
            conn.close()
        sys.exit(0)

    watched_by_series = aggregate_by_series(raw_items)
    log(f"{len(watched_by_series)} unique series/movies touched since last sync")

    log("Fetching AniList library (one call)...")
    user_list, title_index = fetch_user_list()
    log(f"Loaded {len(user_list)} AniList entries, {len(title_index)} title variants indexed")

    updated = skipped = no_change = index_hits = search_hits = 0

    for title, watched in sorted(watched_by_series.items()):
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

        # Turn "N new episodes seen" into an absolute AniList progress number —
        # see module docstring / process()'s docstring for why this can't be an
        # absolute position parsed directly from Netflix's data.
        watched_ep = 1 if watched["watched_format"] == "MOVIE" else entry["progress"] + watched["new_count"]
        watched = {**watched, "episode": watched_ep}

        if not is_plausible_match(entry, watched["watched_format"], watched_ep or None):
            log(f"  ✗ Implausible match, skipping: '{title}' "
                f"(AniList format={entry.get('format')}, total_eps={entry.get('total_episodes')}; "
                f"watched format={watched['watched_format']}, ep={watched_ep})")
            skipped += 1
            continue

        nf_state = nf_state_map.get(media_id)

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

    if conn:
        conn.close()
    log(f"Done — {updated} updated, {no_change} unchanged, {skipped} skipped/unmatched "
        f"({index_hits} index hits, {search_hits} API searches)"
        + (" [DRY RUN — nothing was written]" if DRY_RUN else ""))


if __name__ == "__main__":
    main()
