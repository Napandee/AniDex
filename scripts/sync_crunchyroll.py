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
import json
import os
import sys
import uuid
from datetime import datetime

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from anilist_sync_common import (
    enqueue_outbox_update, find_anilist_id, load_user_list_from_db, load_walk_complete,
    resolve_or_create_user_list_entry, seed_search_cache, season_suffix_candidates,
    set_walk_complete,
)

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
USER_ID = int(os.environ["USER_ID"])
CRUNCHYROLL_ETP_RT = os.environ.get("CRUNCHYROLL_ETP_RT", "")
# Issue #20 — set by run_full_sync.py's Force Full Resync path to bypass the stored
# watermark for one run. Mirrors sync_netflix.py's DRY_RUN env-var pattern.
FORCE_FULL_RESYNC = os.environ.get("FORCE_FULL_RESYNC", "").strip().lower() in ("1", "true", "yes")

# Issue #387, Part 2 — this script's docstring/comments used to say "no DRY_RUN mode,
# so no branch needed here" (see _update()'s old docstring) — that was exactly the gap
# that let today's #352 debugging write real partial state against a live account with
# no safe way to exercise a real fetch first. Same pattern sync_netflix.py already had.
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

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


def _emit_result(entries_updated: int, entries_fetched: int, full_pull: bool) -> None:
    """Issue #46 — the only channel run_full_sync.py has for learning this step's real
    entries-touched count back from the subprocess; it parses this exact prefix out of
    captured stdout. Not emitted in DRY_RUN — that mode is a local investigation tool,
    never invoked through run_full_sync.py."""
    if DRY_RUN:
        return
    print(
        f"SYNC_RESULT: {json.dumps({'entries_updated': entries_updated, 'entries_fetched': entries_fetched, 'full_pull': full_pull})}",
        flush=True,
    )


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

    def fetch_since(self, watermark: datetime | None) -> tuple[list[dict], bool]:
        """Fetch watch history newest-first, stopping once an item at/older than
        `watermark` is hit, or a short page signals the end of history. Returns
        (raw item dicts (unparsed), reached_true_end_of_history).

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
                watched_at = _parse_watched_at(item.get("date_played"))
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


def parse_items(items: list[dict]) -> dict[tuple[str, int], dict]:
    """Returns {(series_title, season_number): {"episode": most_recently_watched_episode, "watched_at": iso_str}}.

    Keyed by (series_title, season_number) rather than series_title alone (issue
    #159): CR's episode_metadata carries season_number, and two seasons of the same
    franchise watched within one sync window must not collapse into a single dict
    entry — previously whichever season had the most recent date_played silently
    won, discarding the other season's progress for that sync run entirely.

    Uses date_played to pick the most recently watched episode per (series, season),
    not the highest episode number — same rule the old file-based history parsing
    used, so rewatches (ep 12 watched months ago, ep 1 watched yesterday) still
    surface ep 1 as the current position and let process() detect the rewatch in
    progress.

    season_number defaults to 1 when CR's episode_metadata omits it or reports
    something non-numeric — matching pre-#159 behavior for any title CR doesn't
    report a season for.
    """
    best: dict[tuple[str, int], dict] = {}
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
        try:
            season = int(float(ep_meta.get("season_number") or 1))
        except (ValueError, TypeError):
            season = 1
        if season < 1:
            season = 1
        key = (title, season)
        watched_at = item.get("date_played") or ""
        if key not in best or watched_at > best[key]["watched_at"]:
            best[key] = {"episode": ep, "watched_at": watched_at}

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
    if conn is None:  # DRY_RUN — no DB writes at all, matching the rest of that mode
        return
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
    if conn is None:  # DRY_RUN — no DB writes at all, matching the rest of that mode
        return
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


def load_title_search_cache(conn) -> dict[str, int | None]:
    """Global (not per-user) AniList title-search cache (issue #115) — a search
    result for a given title string is the same regardless of which user or
    provider is asking, so this is shared across the whole instance. `conn` may
    be None in DRY_RUN, matching that mode's "no DB reads at all" framing."""
    if conn is None:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT title, media_id FROM anilist_title_search_cache")
        return {row["title"]: row["media_id"] for row in cur.fetchall()}


def save_title_search_cache_entry(conn, title: str, media_id: int | None):
    """Persist one newly-resolved (or confirmed-no-match) title immediately, not
    batched at the end of the run — issue #115's whole point is durability across
    an interrupted sync, same principle as #104's walk_complete fix."""
    if conn is None:  # DRY_RUN — no DB writes at all, matching the rest of that mode
        return
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO anilist_title_search_cache (title, media_id) VALUES (%s, %s)
            ON CONFLICT (title) DO UPDATE SET media_id = EXCLUDED.media_id, cached_at = now()
        """, (title, media_id))
    conn.commit()


def load_title_overrides(conn) -> dict[tuple[str, int], int]:
    """Per-user manual title/season overrides (issue #159) — checked in main()'s
    matching loop before the season-aware heuristic/search, so a title the
    heuristic still gets wrong (or leaves unmatched entirely) only ever needs
    correcting once, not every sync. Personal-layer table (cr_title_overrides, see
    schema.sql) — this is the only place any sync job reads it; the web app owns
    all writes (POST /settings/cr-overrides). Keyed by lowercased series_title to
    match case-insensitively, same normalization find_anilist_id()/title_index
    already use — series_title is stored lowercased by the app, but this also
    lowercases at read time defensively in case a row is ever written another way.
    `conn` may be None in DRY_RUN, matching that mode's "no DB reads at all"
    framing — DRY_RUN exercises matching without a real user's manual overrides."""
    if conn is None:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT series_title, season_number, anilist_id FROM cr_title_overrides WHERE user_id = %s",
            (USER_ID,),
        )
        return {
            (row["series_title"].lower(), row["season_number"]): row["anilist_id"]
            for row in cur.fetchall()
        }


def resolve_media_id(title: str, season: int, overrides: dict[tuple[str, int], int],
                      title_index: dict[str, int]) -> dict:
    """Resolve one (title, season) CR watch-history entry to an AniList media id, in
    priority order (issue #159): manual override > season-aware heuristic
    (title_index for a suffixed candidate, then AniList search for one) > bare-title
    fallback (title_index, then AniList search) — the exact order find_anilist_id()
    itself already applies once season_number > 1 is passed through.

    Pulled out of main()'s loop specifically so the "override always wins, before
    any network call" and "season-suffix candidates are tried before the bare
    title" behaviors can be unit tested without a live DB/AniList connection.

    Returns a dict rather than just the id, because main()'s stats counters (index
    vs. search hits) and its anilist_title_search_cache persistence guard (issue
    #115's cache is keyed on the bare title only — caching a season-suffix match's
    id under that key would corrupt it for season-1 lookups later) both need to
    know *how* the id was resolved, not just what it resolved to.
    """
    override_id = overrides.get((title.lower(), season))
    if override_id is not None:
        return {
            "media_id": override_id,
            "matched_via_override": True,
            "in_index_before": False,
            "bare_title_in_index_before": False,
            "via_season_suffix": False,
        }

    normalized = title.lower()
    bare_in_index_before = normalized in title_index
    season_candidates = season_suffix_candidates(title, season) if season > 1 else []
    in_index_before = bare_in_index_before or any(c.lower() in title_index for c in season_candidates)

    media_id = find_anilist_id(title, title_index, season_number=season)

    via_season_suffix = season > 1 and media_id is not None and any(
        title_index.get(c.lower()) == media_id for c in season_candidates
    )
    return {
        "media_id": media_id,
        "matched_via_override": False,
        "in_index_before": in_index_before,
        "bare_title_in_index_before": bare_in_index_before,
        "via_season_suffix": via_season_suffix,
    }


# ── Sync logic ────────────────────────────────────────────────────────────────

def _update(conn, anilist_id: int, **kwargs):
    """Issue #100 — no longer pushes to AniList directly/synchronously; enqueues to
    status_sync_outbox for the app's single shared outbox worker to deliver, same
    local-first pattern issue #18 built for UI bulk-edits. Routed through DRY_RUN
    (issue #387, Part 2) — mirrors sync_netflix.py's own _update() wrapper exactly."""
    if DRY_RUN:
        log(f"    [dry-run] would enqueue outbox update({anilist_id}, {kwargs})")
    else:
        enqueue_outbox_update(conn, anilist_id, "crunchyroll", **kwargs)


def _save_state(conn, anilist_id: int, title: str, last_ep: int, rewatch: bool):
    """save_cr_state(), routed through DRY_RUN for a consistent log line — see
    _update()'s docstring. save_cr_state() itself already guards conn is None
    (safe to call directly), this wrapper exists purely so process()'s DRY_RUN
    output says what it would have saved, matching sync_netflix.py's pattern."""
    if DRY_RUN:
        log(f"    [dry-run] would save state: anilist_id={anilist_id} last_ep={last_ep} rewatch={rewatch}")
    else:
        save_cr_state(conn, anilist_id, title, last_ep, rewatch)


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

    # ── Issue #252: brand-new AniList entry, not yet on the user's list ──────
    # main() only ever builds a synthetic entry (for an incremental sync's
    # unmatched-title case) with status=None — a real AniList entry's status is
    # never None, so this is an unambiguous "create" sentinel. Must be checked
    # before every other branch below: status=None satisfies none of their
    # equality checks, so without this it would silently fall through to the
    # generic progress-advance branch at the bottom, which only sets progress —
    # missing the resolved decision that a newly-created entry defaults to
    # WATCHING, not whatever AniList defaults an id-less SaveMediaListEntry to.
    if status is None:
        _update(conn, anilist_id, progress=cr_ep, status="WATCHING")
        _save_state(conn, anilist_id, title, cr_ep, False)
        return f"new AniList entry created → WATCHING ep {cr_ep}"

    # ── First-time seeing a COMPLETED series in CR history ────────────────────
    # Without prior state we can't safely distinguish "rewatch" from "first sync".
    # Record state and do nothing — next sync will have a baseline.
    if cr_state is None and status == "COMPLETED":
        _save_state(conn, anilist_id, title, cr_ep, False)
        return "first-sync (COMPLETED) — state recorded, no change"

    # ── AniList status already REPEATING but rewatch not recorded in state ────
    # Handles: user changes status to REPEATING in the app/AniList before sync
    # runs. Set rewatch_active so subsequent syncs advance progress correctly.
    # Issue #100 — _update() now enqueues to the outbox rather than pushing to
    # AniList directly, and doesn't commit; call order with _save_state() (which
    # does commit) no longer matters for correctness the way it used to when the
    # update was a network call that could fail mid-flight — both now land in one
    # atomic transaction, so either the outbox row + advanced watermark both
    # persist or neither does. Kept in this order for consistency/minimal diff.
    if status == "REPEATING" and not rewatch_active:
        if cr_ep > al_ep:
            _update(conn, anilist_id, progress=cr_ep)
            _save_state(conn, anilist_id, title, cr_ep, True)
            return f"rewatch detected (already REPEATING) → progress {al_ep} → {cr_ep}"
        _save_state(conn, anilist_id, title, cr_ep, True)
        return "rewatch detected (already REPEATING) — state recorded"

    # ── Rewatch: COMPLETED but CR episode dropped below last-seen ────────────
    # Must come BEFORE the no-change guard: cr_ep < last_ep satisfies that guard
    # and would short-circuit before we ever detect the rewatch.
    if status == "COMPLETED" and cr_ep < (last_ep or total or 999) and not rewatch_active:
        _update(conn, anilist_id, progress=cr_ep, status="REPEATING")
        _save_state(conn, anilist_id, title, cr_ep, True)
        return f"rewatch started → REPEATING ep {cr_ep}"

    # ── Rewatch: a new pass restarted while already mid-rewatch ─────────────
    # Same signal as the branch immediately above, just for a series that's
    # already REPEATING rather than freshly transitioning from COMPLETED: cr_ep
    # only ever reflects genuinely NEW watch activity (fetch_since() already
    # filtered out anything at/before the fetch watermark), so a fresh episode
    # number LOWER than the stored peak (last_ep) can only mean the user
    # rewatched an earlier episode — in practice, almost always "started this
    # rewatch over again from episode 1" while several passes deep already
    # (repeat_count > 0). Without this branch, cr_ep never numerically exceeds
    # last_ep again until the user watches all the way back past the OLD peak,
    # and the final fallback below (`max(cr_ep, last_ep)`) would otherwise
    # silently re-lock last_seen_episode at that stale peak on every single
    # future sync — the exact bug reported in issue #328 (confirmed live: a
    # user rewatching Alderamin on the Sky from episode 1, 6 fresh episodes in
    # one sitting, produced zero AniList updates because last_ep was already
    # sitting at a higher point from an earlier pass).
    if rewatch_active and cr_ep < last_ep:
        _update(conn, anilist_id, progress=cr_ep)
        _save_state(conn, anilist_id, title, cr_ep, True)
        return f"new rewatch pass detected (was at {last_ep}) → progress reset to {cr_ep}"

    # ── No progress since last sync ───────────────────────────────────────────
    if cr_ep <= last_ep and not rewatch_active:
        if cr_ep > al_ep:
            # AniList is behind but we already processed this — shouldn't happen often
            pass
        else:
            _save_state(conn, anilist_id, title, last_ep, rewatch_active)
            return f"no change (CR={cr_ep}, last_seen={last_ep})"

    # ── Rewatch completion: REPEATING and reached total episodes ─────────────
    if rewatch_active and total and cr_ep >= total:
        _update(conn, anilist_id, progress=cr_ep, status="COMPLETED", repeat=repeat + 1)
        _save_state(conn, anilist_id, title, cr_ep, False)
        return f"rewatch complete → COMPLETED (repeat #{repeat + 1})"

    # ── Progress advance for active rewatch ───────────────────────────────────
    if rewatch_active and cr_ep > al_ep:
        _update(conn, anilist_id, progress=cr_ep)
        _save_state(conn, anilist_id, title, cr_ep, True)
        return f"rewatch progress {al_ep} → {cr_ep}"

    # ── DROPPED: user picked it back up ──────────────────────────────────────
    if status == "DROPPED" and cr_ep > last_ep:
        _update(conn, anilist_id, progress=cr_ep, status="CURRENT")
        _save_state(conn, anilist_id, title, cr_ep, False)
        return f"resumed after DROP → CURRENT ep {cr_ep}"

    # ── Normal progress advance (CURRENT, PAUSED) ─────────────────────────────
    if cr_ep > al_ep:
        _update(conn, anilist_id, progress=cr_ep)
        _save_state(conn, anilist_id, title, cr_ep, False)
        return f"progress {al_ep} → {cr_ep}"

    # Nothing to do
    _save_state(conn, anilist_id, title, max(cr_ep, last_ep), rewatch_active)
    return f"AniList ({al_ep}) already at or ahead of CR ({cr_ep})"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Starting Crunchyroll → AniList sync")

    if not CRUNCHYROLL_ETP_RT:
        log("ERROR: Crunchyroll credentials not configured (CRUNCHYROLL_ETP_RT)")
        sys.exit(1)

    if DRY_RUN:
        # No DB touched at all in dry-run — not even the CREATE TABLE IF NOT EXISTS
        # fallback. Treated as a from-scratch first sync (no watermark), which is
        # also the most useful dry-run shape: it exercises the fetch/parse/match/
        # process path (including the create-decision plausibility gate, issue
        # #387) against your full real history without writing anything.
        log("[dry-run] skipping database entirely — no reads, no writes")
        conn = None
        cr_state_map: dict[int, dict] = {}
    else:
        conn = db_connect()
        ensure_table(conn)
        cr_state_map = load_cr_state(conn)
        log(f"Loaded CR sync state for {len(cr_state_map)} series")

    walk_complete = load_walk_complete(conn, "crunchyroll", USER_ID)
    if walk_complete and FORCE_FULL_RESYNC:
        log("FORCE_FULL_RESYNC set — starting a fresh full walk (a previous walk had already completed)")
        set_walk_complete(conn, "crunchyroll", USER_ID, False)  # persisted before the (possibly slow) fetch/process below,
        walk_complete = False            # so an interruption leaves an honest "not complete" state
        watermark = None
    elif walk_complete:
        watermark = compute_fetch_watermark(cr_state_map)
    else:
        log("Full walk not yet complete — re-walking full history this run")
        watermark = None
    # Issue #252 — a full-pull run (initial connect OR a user-triggered Force Full
    # Resync, #20 — both land here via watermark being None) keeps the conservative
    # skip-if-untracked behavior below; only a genuine day-to-day incremental sync
    # (watermark is not None) is allowed to auto-create a new AniList entry.
    full_pull = watermark is None
    log(f"Fetching Crunchyroll watch history since {watermark or '(no watermark — full walk)'}")

    client = CrunchyrollHistory(CRUNCHYROLL_ETP_RT)
    try:
        raw_items, reached_true_end = client.fetch_since(watermark)
    except Exception as e:
        log(f"ERROR: Crunchyroll fetch failed: {e}")
        if conn:
            conn.close()
        sys.exit(1)
    log(f"Fetched {len(raw_items)} new watch-history rows")

    history = parse_items(raw_items)
    log(f"Parsed {len(history)} unique (series, season) combinations from CR history")

    if not history:
        # Issue #104 — safe to mark complete here even though the processing loop
        # below never ran: there's nothing to process, so nothing can be stranded.
        if reached_true_end:
            set_walk_complete(conn, "crunchyroll", USER_ID, True)
            log("Reached true end of Crunchyroll history — full walk marked complete")
        log("No new activity — nothing to do")
        if conn:
            conn.close()
        _emit_result(0, len(raw_items), full_pull)
        sys.exit(0)

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

    # Issue #159 — checked ahead of the season-aware heuristic/search below, so a
    # title the heuristic still gets wrong (or leaves unmatched) only needs fixing
    # once via /settings, not every sync.
    overrides = load_title_overrides(conn)
    log(f"Loaded {len(overrides)} manual title/season overrides")

    updated = skipped = no_change = index_hits = search_hits = override_hits = 0

    for (title, season), data in sorted(history.items()):
        cr_ep = data["episode"]
        # Season suffix only in logs/state's human-readable title — matching itself
        # is keyed on (title, season) throughout, not this label.
        label = title if season <= 1 else f"{title} (season {season})"

        r = resolve_media_id(title, season, overrides, title_index)
        media_id = r["media_id"]

        if r["matched_via_override"]:
            override_hits += 1
        else:
            if (not r["via_season_suffix"] and not r["bare_title_in_index_before"]
                    and title not in title_search_cache):
                # A genuinely new search result this run — persist immediately, not just
                # at the end, so an interrupted run doesn't lose it (same durability
                # principle as #104's walk_complete fix). Skipped for a season-suffix
                # match (see resolve_media_id's docstring) — that would corrupt this
                # bare-title-keyed cache for season-1 lookups later.
                save_title_search_cache_entry(conn, title, media_id)
                title_search_cache[title] = media_id
            if r["in_index_before"] and media_id:
                index_hits += 1
            elif media_id:
                search_hits += 1

        if not media_id:
            log(f"  ✗ No AniList match: '{label}'")
            skipped += 1
            continue

        # Issue #252 — incremental sync: genuinely new watch activity for a title
        # that isn't tracked yet should originate a new AniList entry instead of
        # being silently dropped forever. Full-pull runs (initial connect or a
        # Force Full Resync, #20) keep the original skip behavior. See
        # resolve_or_create_user_list_entry()'s docstring for the full decision.
        # Issue #387 — CR's catalog is anime-only (unlike Netflix/Plex/Prime
        # Video's mostly-non-anime ones), so this script never tracked a
        # watched_format signal and none of today's incident false-positives
        # came from here; passing watched_format=None just means that half of
        # the create-path plausibility check can't fire, same as it always
        # could for any caller. The title-similarity half (the part that
        # actually caught the incident's false positives) still fully applies,
        # for free, now that the check lives inside the shared function instead
        # of being a separate call each provider script had to remember to make.
        decision = resolve_or_create_user_list_entry(
            media_id, title, user_list, full_pull, conn,
            watched_format=None, watched_episode_count=cr_ep,
        )
        if decision == "skip":
            log(f"  ✗ Not in your AniList (or an implausible/unvalidated match): '{label}'")
            skipped += 1
            continue
        if decision == "create":
            log(f"  + Not yet tracked — creating a new AniList entry: '{label}'")

        watched_at = _parse_watched_at(data.get("watched_at"))
        if watched_at:
            save_watermark(conn, media_id, title, watched_at)

        entry = dict(user_list[media_id])
        entry["anilist_id"] = media_id
        cr_state = cr_state_map.get(media_id)

        try:
            result = process(label, cr_ep, entry, cr_state, conn)
            log(f"  '{label}': {result}")
            if "→" in result:
                updated += 1
            else:
                no_change += 1
        except Exception as e:
            log(f"  ERROR processing '{label}': {e}")
            skipped += 1

    # Issue #104 — only mark the walk complete once every fetched title has actually
    # been processed. Marking it right after fetch (the original #97 shape) risked
    # permanently stranding matches if this loop got interrupted partway through: a
    # later sync would trust walk_complete and never re-walk to pick up what was
    # fetched but never reached.
    if reached_true_end:
        set_walk_complete(conn, "crunchyroll", USER_ID, True)
        log("Reached true end of Crunchyroll history — full walk marked complete")

    if conn:
        conn.close()
    log(f"Done — {updated} updated, {no_change} unchanged, {skipped} skipped/unmatched "
        f"({index_hits} index hits, {search_hits} API searches, {override_hits} manual overrides)"
        + (" [DRY RUN — nothing was written]" if DRY_RUN else ""))
    _emit_result(updated, len(raw_items), full_pull)


if __name__ == "__main__":
    main()
