#!/usr/bin/env python3
"""
Crunchyroll → AniList sync (safe, state-aware).

Fetches this user's Crunchyroll watch history directly via CR's own
content/v2/{account_id}/watch-history API (etp_rt cookie login, same auth flow
the vendored crunchyexporter-cli used — see CrunchyrollHistory below), newest-first,
stopping once an item at/older than a stored watermark is reached. This replaced
crunchyexporter-cli's `fetch` step (issue #45): that vendored CLI re-walked the
*entire* CR history from page 1 on every single sync with no watermark at all, and
its local data/history.json lived on the container's ephemeral filesystem anyway
(no volume mount in the deploy's `docker run`), so it never actually accumulated
across redeploys — a Postgres-backed watermark (cr_sync_state.last_seen_watched_at,
migration 004) does. Mirrors the same "fetch newest-first, stop at a per-service
watermark" pattern sync_netflix.py already proved live for issue #48.

Compares freshly-fetched history against the last-known state in Postgres, then
updates AniList with the correct logic:

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

This diff/state-machine logic (process(), below) is deliberately untouched by
issue #45 — only the fetch layer above it changed. The new fetch-side watermark
(last_seen_watched_at) is bookkept separately in main(), via save_watermark(),
never through process()'s own cr_sync_state writes (last_seen_episode /
rewatch_in_progress) — the two update disjoint columns, so call order between
them doesn't matter.

Never goes backwards on progress. Never touches score or notes.

Exit 0 = success, Exit 1 = fatal error.
"""

import base64
import os
import sys
import uuid
from datetime import datetime

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from anilist_sync_common import anilist_update, fetch_user_list, find_anilist_id

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])
CRUNCHYROLL_ETP_RT = os.environ.get("CRUNCHYROLL_ETP_RT", "")
# Issue #20 — set by run_full_sync.py's Force Full Resync path to bypass the stored
# watermark for one run. Mirrors sync_netflix.py's DRY_RUN env-var pattern.
FORCE_FULL_RESYNC = os.environ.get("FORCE_FULL_RESYNC", "").strip().lower() in ("1", "true", "yes")

CR_API_BASE = "https://beta-api.crunchyroll.com"
# Public web client id, embedded in CR's own web app — documented widely in
# open-source CR tools (crunchy-cli, etc.), copied from the vendored
# crunchyexporter-cli source (src/crunchyroll/auth.py at pinned commit
# 1855e567ad1704a6655feedffcf76b1d77e5d690) as the confirmed-working value.
CR_CLIENT_ID = "noaihdevm_6iyg0a8l0q"

PAGE_SIZE = 100
MAX_PAGES = 200  # safety cap — the fetch loop should always stop at the watermark
                  # well before this; this just prevents a runaway loop if CR's
                  # response shape doesn't match what this script expects.


def log(msg):
    print(f"[crunchysync] user={USER_ID} {msg}", flush=True)


# ── Crunchyroll API client ───────────────────────────────────────────────────

class CrunchyrollHistory:
    """Cookie-authenticated content/v2 watch-history client. Auth flow (etp_rt
    login → account_id lookup) matches the vendored crunchyexporter-cli's
    CRAuth.login_with_etp_rt/_fetch_account_id exactly — confirmed working in
    production via that tool prior to issue #45's rewrite. Ordering assumption
    (newest-first) verified via scripts/dev/probe-crunchyroll.sh before this was
    trusted for the watermark early-stop below."""

    def __init__(self, etp_rt: str):
        self.etp_rt = etp_rt
        self.client = httpx.Client(timeout=30)
        self._access_token: str | None = None
        self._account_id: str | None = None

    def _login(self) -> None:
        if self._access_token:
            return
        resp = self.client.post(
            f"{CR_API_BASE}/auth/v1/token",
            headers={"Authorization": f"Basic {base64.b64encode(f'{CR_CLIENT_ID}:'.encode()).decode()}"},
            cookies={"etp_rt": self.etp_rt},
            data={
                "grant_type": "etp_rt_cookie",
                "scope": "offline_access",
                "device_id": str(uuid.uuid4()),
                "device_name": "Chrome on Windows",
                "device_type": "com.crunchyroll.desktop.windows",
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Crunchyroll login failed ({resp.status_code}): {resp.text}")
        self._access_token = resp.json()["access_token"]

        resp = self.client.get(
            f"{CR_API_BASE}/accounts/v1/me",
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        self._account_id = resp.json()["account_id"]

    def _fetch_page(self, page: int) -> list[dict]:
        self._login()
        url = f"{CR_API_BASE}/content/v2/{self._account_id}/watch-history"
        resp = self.client.get(
            url,
            params={"page_size": PAGE_SIZE, "page": page, "locale": "en-US"},
            headers={"Authorization": f"Bearer {self._access_token}"},
        )
        if not resp.is_success:
            raise RuntimeError(f"History fetch failed {resp.status_code}: {resp.text}")
        return resp.json().get("data", [])

    def fetch_since(self, watermark: datetime | None) -> list[dict]:
        """Fetch watch history newest-first, stopping once an item at/older than
        `watermark` is hit, or a short page signals the end of history. Returns
        raw item dicts (unparsed)."""
        items: list[dict] = []

        for page in range(1, MAX_PAGES + 1):
            page_items = self._fetch_page(page)
            if not page_items:
                break

            reached_watermark = False
            for item in page_items:
                watched_at = _parse_watched_at(item.get("date_played"))
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


def _parse_watched_at(raw: str | None) -> datetime | None:
    """CR's `date_played` is an ISO-8601 string — confirmed via the vendored
    source (Episode.watched_at stores it verbatim, and SeriesSummary compares it
    as a raw string, i.e. never parsed/converted anywhere in that codebase)."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_items(items: list[dict]) -> dict[str, dict]:
    """Returns {series_title: {"episode": most_recently_watched_episode, "watched_at": iso_str}}.

    Uses date_played to pick the most recently watched episode per series, not the
    highest episode number — same rule the old file-based history parsing used, so
    rewatches (ep 12 watched months ago, ep 1 watched yesterday) still surface ep 1
    as the current position and let process() detect the rewatch in progress.
    """
    best: dict[str, dict] = {}
    for item in items:
        panel = item.get("panel") or {}
        ep_meta = panel.get("episode_metadata") or {}
        title = (ep_meta.get("series_title") or panel.get("title") or "").strip()
        if not title:
            continue
        try:
            ep = int(float(ep_meta.get("episode_number") or 0))
        except (ValueError, TypeError):
            ep = 0
        if ep == 0:
            continue
        watched_at = item.get("date_played") or ""
        if title not in best or watched_at > best[title]["watched_at"]:
            best[title] = {"episode": ep, "watched_at": watched_at}

    return best


# ── Postgres ──────────────────────────────────────────────────────────────────

def db_connect():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def ensure_table(conn):
    """Defensive fallback if run against a DB that somehow skipped schema.sql/migrations
    — matches the current multi-user schema (composite PK) so it can never create a
    table shape schema.sql wouldn't recognize. In normal operation this is a no-op
    since the table already exists by the time any sync script runs."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cr_sync_state (
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
        # Defensive fallback for a table created before migration 004 landed.
        cur.execute("ALTER TABLE cr_sync_state ADD COLUMN IF NOT EXISTS last_seen_watched_at TIMESTAMPTZ")
    conn.commit()


def load_cr_state(conn) -> dict[int, dict]:
    """Return {anilist_id: {last_seen_episode, rewatch_in_progress, last_seen_watched_at}} for this user."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT anilist_id, last_seen_episode, rewatch_in_progress, last_seen_watched_at "
            "FROM cr_sync_state WHERE user_id = %s",
            (USER_ID,),
        )
        return {row["anilist_id"]: dict(row) for row in cur.fetchall()}


def save_cr_state(conn, anilist_id: int, title: str, last_ep: int, rewatch: bool):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO cr_sync_state (user_id, anilist_id, series_title, last_seen_episode, rewatch_in_progress, last_synced_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (user_id, anilist_id) DO UPDATE SET
                series_title        = EXCLUDED.series_title,
                last_seen_episode   = EXCLUDED.last_seen_episode,
                rewatch_in_progress = EXCLUDED.rewatch_in_progress,
                last_synced_at      = now()
        """, (USER_ID, anilist_id, title, last_ep, rewatch))
    conn.commit()


def save_watermark(conn, anilist_id: int, title: str, watched_at: datetime):
    """Fetch-side watermark bookkeeping only (issue #45) — updates just
    last_seen_watched_at, kept fully separate from save_cr_state()'s columns
    (last_seen_episode/rewatch_in_progress) so this never touches process()'s own
    state writes: the two functions' UPDATE SET clauses are column-disjoint, so
    call order between them within a single sync run doesn't matter. Takes the max
    with whatever's already stored so this can't regress the watermark."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO cr_sync_state (user_id, anilist_id, series_title, last_seen_watched_at, last_synced_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (user_id, anilist_id) DO UPDATE SET
                last_seen_watched_at = GREATEST(
                    COALESCE(cr_sync_state.last_seen_watched_at, EXCLUDED.last_seen_watched_at),
                    EXCLUDED.last_seen_watched_at
                )
        """, (USER_ID, anilist_id, title, watched_at))
    conn.commit()


def compute_fetch_watermark(state_map: dict[int, dict]) -> datetime | None:
    """The single cursor fetch_since() paginates against — the newest
    last_seen_watched_at across all series from the previous sync. Mirrors
    sync_netflix.py's compute_fetch_watermark exactly: CR's watch-history feed is
    one chronological stream across all titles, so one watermark is enough to know
    when pagination has caught up, even though state is still tracked per-series."""
    values = [s["last_seen_watched_at"] for s in state_map.values() if s.get("last_seen_watched_at")]
    return max(values) if values else None


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
    # anilist_update() must run BEFORE save_cr_state(), not after — if the write
    # throws (a transient AniList error, e.g. rate-limiting), saving state anyway
    # would mark this watermark as handled while the real progress update never
    # landed, and since the sync only re-considers activity newer than the stored
    # watermark, that miss would never get retried on a later run. Confirmed live
    # via the identical bug in sync_netflix.py's equivalent branch (issue #48).
    if status == "REPEATING" and not rewatch_active:
        if cr_ep > al_ep:
            anilist_update(anilist_id, progress=cr_ep)
            save_cr_state(conn, anilist_id, title, cr_ep, True)
            return f"rewatch detected (already REPEATING) → progress {al_ep} → {cr_ep}"
        save_cr_state(conn, anilist_id, title, cr_ep, True)
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

    if not CRUNCHYROLL_ETP_RT:
        log("ERROR: Crunchyroll credentials not configured (CRUNCHYROLL_ETP_RT)")
        sys.exit(1)

    conn = db_connect()
    ensure_table(conn)
    cr_state_map = load_cr_state(conn)
    log(f"Loaded CR sync state for {len(cr_state_map)} series")

    watermark = compute_fetch_watermark(cr_state_map)
    if FORCE_FULL_RESYNC:
        log("FORCE_FULL_RESYNC set — ignoring stored watermark, re-fetching full history")
        watermark = None
    log(f"Fetching Crunchyroll watch history since {watermark or '(no watermark — first sync, full pull)'}")

    client = CrunchyrollHistory(CRUNCHYROLL_ETP_RT)
    try:
        raw_items = client.fetch_since(watermark)
    except Exception as e:
        log(f"ERROR: Crunchyroll fetch failed: {e}")
        conn.close()
        sys.exit(1)
    log(f"Fetched {len(raw_items)} new watch-history rows")

    history = parse_items(raw_items)
    log(f"Parsed {len(history)} unique series from CR history")

    if not history:
        log("No new activity — nothing to do")
        conn.close()
        sys.exit(0)

    log("Fetching AniList library (one call)...")
    user_list, title_index = fetch_user_list()
    log(f"Loaded {len(user_list)} AniList entries, {len(title_index)} title variants indexed")

    updated = skipped = no_change = index_hits = search_hits = 0

    for title, data in sorted(history.items()):
        cr_ep = data["episode"]
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

        watched_at = _parse_watched_at(data.get("watched_at"))
        if watched_at:
            save_watermark(conn, media_id, title, watched_at)

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
