#!/usr/bin/env python3
"""
Netflix → AniList sync (cookie-replay, incremental, state-aware).

Fetches this user's Netflix viewing activity via cookie-replay auth against
Netflix's internal "Shakti" API (no public API for this — see issue #48 and
notes/2026-08-14-netflix-prime-sync-research.md on feature/netflix-prime-sync
for the sourcing), newest-first, stopping once an item at/older than the
stored watermark is reached. Diffs against last-known state in Postgres, then
updates AniList with the same discipline sync_crunchyroll.py uses: never
creates new AniList entries, never goes backwards on progress, never touches
score or notes.

REVERSE-ENGINEERED API SHAPE — NOT YET VERIFIED AGAINST A LIVE ACCOUNT.
The viewingactivity response field names below (`viewedItems`, `date`,
`seriesTitle`, `title`, `episode`) are this script's best-effort mapping,
credited to statsoflife/extract-netflix-activity's documented shape. No real
Netflix session was available to confirm them from this environment. The
first real run against a live account should confirm or correct
NetflixHistory._resolve_build_id() and the _item_*() parsing helpers below.

Unlike sync_crunchyroll.py (which loads a full history dump and compares
episode numbers against a remembered last_seen_episode), this script only
ever sees genuinely new data — fetch_since() already filters out anything at
or before the stored watermark — so netflix_sync_state tracks
last_seen_watched_at (a timestamp) instead of last_seen_episode, and rewatch
detection compares the newly-observed episode against AniList's own current
progress rather than a separately tracked episode number.

Exit 0 = success, Exit 1 = fatal error. Matches the other scripts/sync_*.py
scripts' contract.
"""

import os
import re
import sys
from datetime import datetime, timezone

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from anilist_sync_common import anilist_update, fetch_user_list, find_anilist_id, is_plausible_match

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])
NETFLIX_ID_COOKIE = os.environ.get("NETFLIX_ID_COOKIE", "")
NETFLIX_SECURE_ID_COOKIE = os.environ.get("NETFLIX_SECURE_ID_COOKIE", "")

# When set, logs every AniList update / state write this run *would* make instead of
# making it — the Shakti field-name assumptions below are reverse-engineered and
# unverified against a live account, so the first real run against real credentials
# should go through here first rather than risk writing bad progress to AniList.
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

PAGE_SIZE = 20
MAX_PAGES = 500  # safety cap — the fetch loop should always stop at the watermark
                  # well before this; this just prevents a runaway loop if Netflix's
                  # response shape doesn't match what's expected here.


def log(msg):
    print(f"[netflixsync] user={USER_ID} {msg}", flush=True)


# ── Netflix Shakti client ────────────────────────────────────────────────────

class NetflixHistory:
    """Cookie-authenticated Shakti viewingactivity client."""

    def __init__(self, netflix_id_cookie: str, secure_netflix_id_cookie: str):
        self.client = httpx.Client(
            cookies={"NetflixId": netflix_id_cookie, "SecureNetflixId": secure_netflix_id_cookie},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
            follow_redirects=True,
        )
        self._build_id: str | None = None

    def _resolve_build_id(self) -> str:
        """The API path includes a build_id that changes with Netflix's deploys, so it
        can't be stored as a static credential — resolve it fresh each run from a
        logged-in page's embedded state, same trick as the source project."""
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

    def fetch_since(self, watermark: datetime | None) -> list[dict]:
        """Fetch viewing history newest-first, stopping once an item at/older than
        `watermark` is hit. Returns raw viewedItems dicts (unparsed)."""
        build_id = self._resolve_build_id()
        url = f"https://www.netflix.com/api/shakti/{build_id}/viewingactivity"
        items: list[dict] = []

        for page in range(MAX_PAGES):
            resp = self.client.get(url, params={"pg": page, "pgSize": PAGE_SIZE})
            resp.raise_for_status()
            data = resp.json()
            page_items = data.get("viewedItems") or []
            if not page_items:
                break

            reached_watermark = False
            for item in page_items:
                watched_at = _item_watched_at(item)
                if watermark and watched_at and watched_at <= watermark:
                    reached_watermark = True
                    break
                items.append(item)
            if reached_watermark:
                break
        else:
            log(f"WARNING: hit the {MAX_PAGES}-page safety cap without reaching the "
                f"watermark — response shape may not match what this script expects.")

        return items


def _item_watched_at(item: dict) -> datetime | None:
    """Shakti's `date` field is epoch milliseconds."""
    ms = item.get("date")
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _is_episode(item: dict) -> bool:
    return bool(item.get("seriesTitle"))


def _item_episode_number(item: dict) -> int:
    # Reverse-engineered field: try a structured "episode" index first, fall back to
    # parsing it out of a combined "Show: Season X: Episode Y" title string.
    ep = item.get("episode")
    if isinstance(ep, int):
        return ep
    m = re.search(r"Episode\s+(\d+)", item.get("title") or "")
    return int(m.group(1)) if m else 0


def aggregate_by_series(items: list[dict]) -> dict[str, dict]:
    """Returns {title: {watched_at, episode, watched_format}}, picking the most
    recently watched item per series/movie — same rewatch-safe selection logic as
    sync_crunchyroll.py's load_history()."""
    best: dict[str, dict] = {}
    for item in items:
        is_ep = _is_episode(item)
        title = ((item.get("seriesTitle") if is_ep else item.get("title")) or "").strip()
        if not title:
            continue
        watched_at = _item_watched_at(item)
        if title not in best or (watched_at or datetime.min.replace(tzinfo=timezone.utc)) > best[title]["watched_at"]:
            best[title] = {
                "watched_at": watched_at or datetime.min.replace(tzinfo=timezone.utc),
                "episode": _item_episode_number(item) if is_ep else 1,
                "watched_format": "TV" if is_ep else "MOVIE",
            }
    return best


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

    Every call here represents genuinely new activity (fetch_since() already
    filtered out anything at/older than the stored watermark) — there is no
    "no change" branch based on comparing against a remembered episode number the
    way sync_crunchyroll.py has one; the comparison baseline is AniList's own
    current progress (al_ep).
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

    # ── Rewatch: COMPLETED but the newly watched episode is below AniList's own
    # progress (mirrors sync_crunchyroll.py's rewatch-start detection, using al_ep
    # as the baseline instead of a tracked last_seen_episode).
    if status == "COMPLETED" and watched_ep and watched_ep < al_ep and not rewatch_active:
        _update(anilist_id, progress=watched_ep, status="REPEATING")
        _save_state(conn, anilist_id, title, watched_at, True)
        return f"rewatch started → REPEATING ep {watched_ep}"

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

    if not NETFLIX_ID_COOKIE or not NETFLIX_SECURE_ID_COOKIE:
        log("ERROR: Netflix cookies not configured (NETFLIX_ID_COOKIE / NETFLIX_SECURE_ID_COOKIE)")
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

    client = NetflixHistory(NETFLIX_ID_COOKIE, NETFLIX_SECURE_ID_COOKIE)
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

        if not is_plausible_match(entry, watched["watched_format"], watched["episode"] or None):
            log(f"  ✗ Implausible match, skipping: '{title}' "
                f"(AniList format={entry.get('format')}, total_eps={entry.get('total_episodes')}; "
                f"watched format={watched['watched_format']}, ep={watched['episode']})")
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
