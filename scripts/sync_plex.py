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
matching is the fallback path; see the Guid-based fast path below.

Guid-based fast path (issue #447): a Plex library item's `Guid` list — NOT
present on /status/sessions/history/all's own response, only on the fuller
`/library/metadata/{ratingKey}` item detail, hence PlexHistory.fetch_item_guids()
below — can carry an AniDB or MAL id when the user has an anime-specific
metadata agent installed:
  - HAMA (github.com/ZeroQI/Hama.bundle): `com.plexapp.agents.hama://anidb-<id>`
  - MyAnimeList.bundle (github.com/Fribb/MyAnimeList.bundle): confirmed by
    reading its Info.plist (CFBundleIdentifier =
    `net.fribbtastic.coding.plex.myanimelist`, which is NOT the
    `com.plexapp.agents.*` form HAMA uses — a legacy Plex Framework2 agent's
    guid is always `<its own CFBundleIdentifier>://<id>`, not a fixed
    `com.plexapp.agents.` prefix) and its update() method (uses `metadata.id`,
    which search()/JIKAN_UTILS.search() sets to the raw MyAnimeList id) —
    guids look like `net.fribbtastic.coding.plex.myanimelist://<mal_id>`.
Resolved against anidb_mal_mapping_cache (scripts/sync_id_mappings.py, sourced
from Fribb/anime-lists — confirmed live 2026-09-01: real, actively-maintained,
42,870-row dataset, no license file). This is checked once per (title, season)
group ahead of title matching — a strictly better signal where present, since
it's an exact id match rather than a heuristic — and title matching remains
the guaranteed fallback for every user without one of these agents installed,
or when the id isn't in the mapping cache. See
notes/2026-08-19-plex-sync-research.md, section 3, for the original research
this was deferred from (issue #153).

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

import os
import re
import sys
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

from anilist_sync_common import (
    compute_fetch_watermark as _compute_fetch_watermark,
    db_connect, determine_full_pull_watermark, emit_sync_result,
    enqueue_outbox_update, find_anilist_id, is_plausible_match,
    load_title_search_cache, load_user_list_from_db, make_logger,
    mark_walk_complete_if_reached_end, process_max_aggregated_progress,
    resolve_or_create_user_list_entry, save_provider_state, save_provider_watermark,
    save_title_search_cache_entry, season_suffix_candidates, seed_search_cache,
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

# Issue #447 — the AniDB/MAL Guid prefixes this app resolves via
# anidb_mal_mapping_cache. See the module docstring above for how each was
# confirmed.
_HAMA_ANIDB_RE = re.compile(r"^com\.plexapp\.agents\.hama://anidb-(\d+)")
_MYANIMELIST_BUNDLE_MAL_RE = re.compile(r"^net\.fribbtastic\.coding\.plex\.myanimelist://(\d+)")


log = make_logger("plexsync", USER_ID)


def _emit_result(entries_updated: int, entries_fetched: int, full_pull: bool) -> None:
    """emit_sync_result(), routed through this script's own DRY_RUN — see
    anilist_sync_common.emit_sync_result()'s docstring (issue #46/#414)."""
    emit_sync_result(entries_updated, entries_fetched, full_pull, DRY_RUN)


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

    def fetch_item_guids(self, rating_key: str) -> list[str]:
        """GET /library/metadata/{ratingKey} — the fuller item-detail endpoint
        that carries the `Guid` list (issue #447), unlike the lightweight
        history rows fetch_since() above returns. Best-effort: any failure
        (network error, unexpected shape) returns an empty list rather than
        raising, since the Guid fast path is opportunistic — a failure here
        must never block the title-matching fallback that already works."""
        try:
            resp = self.client.get(f"{self.base_url}/library/metadata/{rating_key}")
            resp.raise_for_status()
            metadata_list = (resp.json().get("MediaContainer") or {}).get("Metadata") or []
            if not metadata_list:
                return []
            guids = metadata_list[0].get("Guid") or []
            return [g["id"] for g in guids if isinstance(g, dict) and g.get("id")]
        except Exception:
            return []


def _item_watched_at(item: dict) -> datetime | None:
    ts = item.get("viewedAt")
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def _is_episode(item: dict) -> bool:
    return item.get("type") == "episode"


def parse_items(items: list[dict]) -> dict[tuple[str, int], dict]:
    """Returns {(series_title, season_number): {"episode": most_recently_watched_episode,
    "watched_at": datetime, "rating_key": ...}} — same "most recently watched
    wins, not highest episode number" rule as sync_crunchyroll.py's
    parse_items() (so a rewatch started from episode 1 surfaces as episode 1
    for process() to detect), keyed by (title, season) for the same reason
    issue #159 keyed CR's parser that way: two seasons of the same franchise
    watched in one sync window must not collapse into a single entry.

    season_number defaults to 1 when parentIndex is absent/non-numeric (movies
    have no parentIndex at all).

    rating_key (issue #447) is the series' own ratingKey for an episode
    (`grandparentRatingKey` — a standard Plex field alongside `grandparentTitle`,
    which this parser already relies on) or the item's own `ratingKey` for a
    movie — used by the Guid fast path to fetch that item's full detail
    (history rows themselves don't carry a Guid list)."""
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
        rating_key = (item.get("grandparentRatingKey") if is_ep else item.get("ratingKey"))
        existing = best.get(key)
        if not existing or watched_at > existing["watched_at"]:
            best[key] = {
                "episode": ep,
                "watched_at": watched_at,
                "watched_format": "MOVIE" if not is_ep else "TV",
                "rating_key": rating_key,
            }

    return best


# ── Guid-based fast path (issue #447) ───────────────────────────────────────

def parse_agent_guid_ids(guids: list[str]) -> tuple[int | None, int | None]:
    """Returns (anidb_id, mal_id) parsed out of a Plex item's Guid list — either
    may be None if that agent's id isn't present. Pure/no I/O so it's directly
    testable without a live Plex server or database."""
    anidb_id = mal_id = None
    for guid in guids:
        if anidb_id is None:
            m = _HAMA_ANIDB_RE.match(guid)
            if m:
                anidb_id = int(m.group(1))
        if mal_id is None:
            m = _MYANIMELIST_BUNDLE_MAL_RE.match(guid)
            if m:
                mal_id = int(m.group(1))
    return anidb_id, mal_id


def resolve_anilist_id_from_guids(guids: list[str], mapping: dict[str, dict[int, int]]) -> int | None:
    """Resolves a Plex item's Guid list straight to an AniList id via the
    cached mapping table, or None if neither agent id is present or neither
    resolves. `mapping` is {"anidb": {anidb_id: anilist_id}, "mal": {mal_id:
    anilist_id}} — see load_id_mapping_cache(). MAL checked first: issue
    #447's own research found MAL id coverage in the upstream dataset (91% of
    AniList-tagged rows) meaningfully higher than AniDB's (63%), so it's the
    more likely of the two to actually resolve when both happen to be
    present, though either is an equally trustworthy exact-id match once
    found."""
    anidb_id, mal_id = parse_agent_guid_ids(guids)
    if mal_id is not None and mal_id in mapping["mal"]:
        return mapping["mal"][mal_id]
    if anidb_id is not None and anidb_id in mapping["anidb"]:
        return mapping["anidb"][anidb_id]
    return None


def load_id_mapping_cache(conn) -> dict[str, dict[int, int]]:
    """Loads anidb_mal_mapping_cache (scripts/sync_id_mappings.py) into two
    plain dicts for O(1) lookup during the matching loop — same "load once up
    front, dict lookups per item" shape as load_user_list_from_db()/
    load_title_search_cache() already use in this script."""
    mapping: dict[str, dict[int, int]] = {"anidb": {}, "mal": {}}
    if conn is None:
        return mapping
    with conn.cursor() as cur:
        cur.execute("SELECT anilist_id, anidb_id, mal_id FROM anidb_mal_mapping_cache")
        for row in cur.fetchall():
            if row["anidb_id"] is not None:
                mapping["anidb"][row["anidb_id"]] = row["anilist_id"]
            if row["mal_id"] is not None:
                mapping["mal"][row["mal_id"]] = row["anilist_id"]
    return mapping


# ── Postgres ──────────────────────────────────────────────────────────────────

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
    """save_provider_state() bound to plex_sync_state (issue #414) — kept as its
    own function (rather than a bare re-export) so tests can still monkeypatch
    sync_plex.save_plex_state directly, same as before this consolidation."""
    save_provider_state(conn, "plex_sync_state", USER_ID, anilist_id, title, last_ep, rewatch)


def save_watermark(conn, anilist_id: int, title: str, watched_at: datetime):
    """save_provider_watermark() bound to plex_sync_state (issue #414) — see its
    docstring for why this stays column-disjoint from save_plex_state()."""
    save_provider_watermark(conn, "plex_sync_state", USER_ID, anilist_id, title, watched_at)


def compute_fetch_watermark(state_map: dict[int, dict]) -> datetime | None:
    """Re-exported under this script's own name (issue #414) — see
    sync_crunchyroll.py's identical wrapper for why."""
    return _compute_fetch_watermark(state_map)


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
    """The actual branching logic (state machine) is shared with
    sync_crunchyroll.py/sync_primevideo.py via
    anilist_sync_common.process_max_aggregated_progress() (issue #414) — this
    wrapper just binds it to Plex's own _update()/_save_state() (so DRY_RUN and
    test monkeypatching of those still work exactly as before) and label ("Plex")."""
    anilist_id = entry["anilist_id"]

    def update_fn(**kwargs):
        _update(conn, anilist_id, **kwargs)

    def save_state_fn(last_ep, rewatch):
        _save_state(conn, anilist_id, title, last_ep, rewatch)

    return process_max_aggregated_progress(
        title, plex_ep, entry, plex_state, "Plex", update_fn, save_state_fn,
    )


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

    watermark, full_pull = determine_full_pull_watermark(
        conn, "plex", USER_ID, state_map, FORCE_FULL_RESYNC, log,
    )
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
        mark_walk_complete_if_reached_end(conn, "plex", USER_ID, reached_true_end, "Plex", log)
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

    id_mapping = load_id_mapping_cache(conn)
    log(f"Loaded {len(id_mapping['anidb'])} AniDB / {len(id_mapping['mal'])} MAL id mappings")

    updated = skipped = no_change = index_hits = search_hits = guid_hits = 0

    for (title, season), watched in sorted(watched_by_key.items()):
        # Issue #447 — Guid-based fast path, checked ahead of title matching.
        # Best-effort: fetch_item_guids() never raises (see its own docstring),
        # so a missing rating_key or a failed/empty fetch just falls through to
        # title matching below exactly as if this block didn't exist.
        media_id = None
        rating_key = watched.get("rating_key")
        if rating_key:
            guids = client.fetch_item_guids(str(rating_key))
            if guids:
                media_id = resolve_anilist_id_from_guids(guids, id_mapping)
                if media_id:
                    guid_hits += 1
                    log(f"  ✓ Guid fast-path match: '{title}' -> AniList #{media_id}")

        normalized = title.lower()
        candidates = season_suffix_candidates(title, season) if season > 1 else []
        in_index_before = normalized in title_index or any(c.lower() in title_index for c in candidates)

        if not media_id:
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

    mark_walk_complete_if_reached_end(conn, "plex", USER_ID, reached_true_end, "Plex", log)

    if conn:
        conn.close()
    log(f"Done — {updated} updated, {no_change} unchanged, {skipped} skipped/unmatched "
        f"({guid_hits} guid fast-path hits, {index_hits} index hits, {search_hits} API searches)"
        + (" [DRY RUN — nothing was written]" if DRY_RUN else ""))
    _emit_result(updated, len(raw_items), full_pull)


if __name__ == "__main__":
    main()
