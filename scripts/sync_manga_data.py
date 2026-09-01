#!/usr/bin/env python3
"""
Standalone global job — syncs manga/light-novel "living integration" data
(status, latest chapter/volume, English licensor) into manga_adaptation_cache
(issue #454, direction decided in spike #450). Catalog-wide like
sync_filler_data.py / sync_airing_schedule.py, not per-user: one pass over
every distinct anime.id already in the local catalog.

Three-source pipeline, chosen specifically to avoid the fuzzy-title-matching
risk #387's incident (14/16 false-positive auto-created entries) warns
against — every hop after the first is validated against a real id, not a
similarity score:

  1. AniList's own `relations` field on the anime itself — a `SOURCE`-type
     edge (querying FROM the anime, so the manga/novel is its "source", not
     an "adaptation" — that direction is only how the edge reads from the
     manga's own side) pointing at a MANGA/NOVEL/ONE_SHOT node gives the
     source material's own AniList id + title, no matching needed at all.
  2. MangaDex (api.mangadex.org, no auth) — searched by that title, but only
     trusted if the hit's `attributes.links.al` field equals the AniList
     source id from step 1 exactly. MangaDex's `links` field also carries
     `mu`, a MangaUpdates URL slug, confirmed live to always match the same
     series MangaUpdates itself would return.
  3. MangaUpdates (api.mangaupdates.com/v1, no auth) — has no slug-lookup
     endpoint (confirmed against their own docs), so this searches by the
     same title, then keeps only the result whose own `url` contains the
     slug from step 2 — again an exact substring match, not a similarity
     score. That result's real numeric series_id then gets a full
     GET /series/{id} call for latest_chapter/last_updated/licensed/
     publishers.

A title that fails step 2 or 3 (no MangaDex hit, no matching MangaUpdates
slug, or either request erroring) still gets real data — falls back to
AniList's own `status` field plus whichever of its `externalLinks` is typed
`language: "English"`, recorded as match_method='anilist_only' rather than
'mangadex_verified' so a future UI/debugging pass can tell which rows have
the richer data and which don't.

Deliberately never surfaces MangaDex's own chapter-feed or scanlation-group
content/links — only the small set of `links`/`status` metadata fields named
above. See #450's own research comment for why (MangaDex's terms require
crediting scanlation groups specifically when their content is surfaced;
staying out of that scope entirely is simpler and keeps this pointed at
official-release data, matching the rest of this app's AniList-first stance).

Runs weekly (not daily — chapter/status data doesn't need same-day
freshness, unlike the hourly airing-schedule refresh) — see app/main.py's
`manga_data_refresh` scheduler job.

Usage:
    python scripts/sync_manga_data.py

Requires .env (or env vars) with: DATABASE_URL
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

ANILIST_API = "https://graphql.anilist.co"
MANGADEX_API = "https://api.mangadex.org"
MANGAUPDATES_API = "https://api.mangaupdates.com/v1"

# Polite inter-request pauses. MangaDex's own docs state a real, enforced ~5
# req/s global limit — 0.25s keeps this comfortably under that. MangaUpdates
# states no specific number, only "reasonable spacing... employ caching" — 0.6s
# matches the same order-of-magnitude politeness sync_filler_data.py already
# uses against AniFillerPedia's similarly-unspecified limit. Both services have
# IP-banned aggressive scrapers before (confirmed via #450's own research), so
# this errs conservative rather than trying to find the real ceiling.
#
# ANILIST_SLEEP — issue #454's own first production run found this wrong: 0.5s
# is 2 req/s = 120 req/min, which is ABOVE AniList's real 90 req/min limit
# (anilist_sync_common.py's own gql() comment), not under it. That run
# 429'd on the large majority of a real ~1500-title catalog. 0.8s (75 req/min)
# leaves real margin, and AniList's limit is shared across the whole app (any
# concurrent user-facing quick-add-by-title call competes for the same
# budget), so real retry-with-backoff below matters at least as much as the
# sleep interval itself — a "polite enough" fixed delay alone isn't sufficient
# in a multi-consumer environment the way it might be for a service this app
# is the only real user of.
ANILIST_SLEEP = 0.8
MANGADEX_SLEEP = 0.25
MANGAUPDATES_SLEEP = 0.6

# Same retry-with-backoff shape as anilist_sync_common.py's gql() — not reused
# directly (that module requires ANILIST_TOKEN/ANILIST_USERNAME at import time,
# a per-user-script contract this catalog-wide script, like
# sync_filler_data.py/sync_airing_schedule.py, deliberately doesn't have) but
# the same real-world reason applies: AniList's rate limit is genuinely hit in
# practice, confirmed live during both #48's original testing and #454's own
# first production run.
MAX_ANILIST_RATE_LIMIT_RETRIES = 5

# Same asymmetric-cooldown shape as sync_filler_data.py's RECHECK_INTERVAL_*:
# a title with a matched adaptation is worth rechecking on every weekly run —
# tracking new chapters is the whole point — while a title with no adaptation
# found is unlikely to suddenly gain a brand-new AniList relation, so it gets
# a much longer cooldown.
RECHECK_INTERVAL_MATCHED = timedelta(days=6)  # just under the weekly cadence
RECHECK_INTERVAL_NO_MATCH = timedelta(days=30)

RELATIONS_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    relations {
      edges {
        relationType(version: 2)
        node { id type title { romaji english } }
      }
    }
  }
}
"""

SOURCE_MEDIA_QUERY = """
query ($id: Int) {
  Media(id: $id) {
    id
    format
    status
    chapters
    volumes
    externalLinks { url site type language }
  }
}
"""

SOURCE_FORMATS = {"MANGA", "NOVEL", "ONE_SHOT"}


def _anilist_gql(query: str, variables: dict) -> dict:
    """Same retry-with-backoff shape as anilist_sync_common.py's gql() — see
    MAX_ANILIST_RATE_LIMIT_RETRIES' comment for why this isn't just imported
    from there directly. Respects AniList's own Retry-After header rather than
    a fixed guess."""
    resp = None
    for attempt in range(MAX_ANILIST_RATE_LIMIT_RETRIES):
        resp = httpx.post(
            ANILIST_API,
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code == 429 and attempt < MAX_ANILIST_RATE_LIMIT_RETRIES - 1:
            wait = float(resp.headers.get("retry-after", 5))
            print(f"    AniList rate-limited — waiting {wait}s before retry...", flush=True)
            time.sleep(wait)
            continue
        break

    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"AniList error: {data['errors']}")
    return data["data"]


def fetch_source_candidates(anilist_anime_id: int) -> list[dict]:
    """Every SOURCE-type relation (the manga/light novel this anime adapts) on
    the anime itself. Returns [{"id", "title", "format"}] — format one of
    MANGA/NOVEL/ONE_SHOT; other formats (e.g. a SOURCE edge to a VISUAL_NOVEL
    or GAME) are filtered out, since this app only tracks the two source
    types issue #454 scopes for."""
    data = _anilist_gql(RELATIONS_QUERY, {"id": anilist_anime_id})
    media = data.get("Media")
    if not media:
        return []
    out = []
    for edge in (media.get("relations") or {}).get("edges", []):
        node = edge.get("node") or {}
        if edge.get("relationType") != "SOURCE" or node.get("type") != "MANGA":
            continue
        # AniList's `type` enum only has ANIME/MANGA at the top level — NOVEL and
        # ONE_SHOT are `format` values on a MANGA-type node, not separate `type`s.
        # The actual format filter happens in fetch_source_metadata() below, once
        # the node's real format is fetched; this pass just narrows to "some
        # MANGA-type SOURCE node exists."
        title = (node.get("title") or {}).get("english") or (node.get("title") or {}).get("romaji") or ""
        if not title or not node.get("id"):
            continue
        out.append({"id": node["id"], "title": title})
    return out


def fetch_source_metadata(source_id: int) -> dict | None:
    """Real status/chapters/volumes/externalLinks/format for one candidate source
    id — the relations query above only gives id+title, same "fetch real metadata
    before trusting a candidate" precedent anilist_sync_common.py's
    fetch_anilist_media_metadata() already established for #387. Returns None (skip
    this candidate) if the format turns out not to be one this app tracks."""
    data = _anilist_gql(SOURCE_MEDIA_QUERY, {"id": source_id})
    media = data.get("Media")
    if not media:
        return None
    return media


def english_licensor_from_links(external_links: list[dict]) -> tuple[str | None, str | None]:
    """First externalLinks entry typed language="English" — same "community-
    curated, may lag reality" caveat CLAUDE.md's Data Source section already
    documents for this app's existing streaming-link display. Prefers a
    STREAMING-type link (an actual official reading site) over an INFO-type one
    (a publisher's general info page, not necessarily where you'd read it)."""
    english = [lnk for lnk in external_links if (lnk.get("language") or "") == "English"]
    if not english:
        return None, None
    streaming = [lnk for lnk in english if lnk.get("type") == "STREAMING"]
    best = streaming[0] if streaming else english[0]
    return best.get("site"), best.get("url")


def resolve_mangadex_match(title: str, anilist_source_id: int) -> dict | None:
    """Search MangaDex by title, keep only a hit whose own links.al equals the
    AniList source id we already have — a hard equality check, not a similarity
    score, deliberately avoiding the kind of fuzzy title-match risk #387's
    incident warns against. Returns the hit's {"id", "mu_slug"} or None.

    Confirmed live during #454's own build: this resolves well for MANGA-format
    sources, but rarely for NOVEL ones — MangaDex hosts manga *adaptations* of
    light novels (each with its own distinct AniList id), not the light novel's
    prose text itself, so a search rarely turns up a hit whose links.al matches
    the light novel's own AniList id. A NOVEL-type source falling back to
    match_method='anilist_only' most of the time is the expected, honest
    outcome here, not a bug to chase — see fetch_source_metadata()'s caller for
    what that fallback still provides (real status + externalLinks-derived
    licensor, just no MangaUpdates chapter/release data)."""
    resp = httpx.get(
        f"{MANGADEX_API}/manga",
        params={"title": title, "limit": 5},
        timeout=20,
    )
    if not resp.is_success:
        return None
    for item in resp.json().get("data", []):
        links = (item.get("attributes") or {}).get("links") or {}
        al = links.get("al")
        if al is not None and str(al) == str(anilist_source_id):
            mu_slug = links.get("mu")
            return {"id": item.get("id"), "mu_slug": mu_slug}
    return None


def resolve_mangaupdates_series_id(title: str, mu_slug: str) -> int | None:
    """Search MangaUpdates by title, keep only a result whose own url contains
    the mu_slug MangaDex already gave us — an exact substring match, not a
    similarity score. MangaUpdates has no slug-lookup endpoint of its own
    (confirmed against their docs), so this is the only way to resolve a slug
    to the real numeric series_id their /series/{id} endpoint actually needs."""
    if not mu_slug:
        return None
    resp = httpx.post(
        f"{MANGAUPDATES_API}/series/search",
        json={"search": title, "perpage": 10},
        timeout=20,
    )
    if not resp.is_success:
        return None
    for result in resp.json().get("results", []):
        record = result.get("record") or {}
        if mu_slug in (record.get("url") or ""):
            return record.get("series_id")
    return None


def fetch_mangaupdates_series(series_id: int) -> dict | None:
    resp = httpx.get(f"{MANGAUPDATES_API}/series/{series_id}", timeout=20)
    if not resp.is_success:
        return None
    return resp.json()


def english_publisher_from_mangaupdates(series: dict) -> str | None:
    for pub in series.get("publishers") or []:
        if pub.get("type") == "English":
            return pub.get("publisher_name")
    return None


# ── Postgres ──────────────────────────────────────────────────────────────────

def db_connect():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def fetch_catalog_anime_ids(conn) -> list[int]:
    """Every distinct anime already in the local catalog — same "whole global
    anime table, not scoped to any one user" shape as sync_filler_data.py's
    fetch_catalog_anime_ids()."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM anime ORDER BY id")
        return [row["id"] for row in cur.fetchall()]


def load_sync_state(conn) -> dict[int, tuple[bool, datetime]]:
    with conn.cursor() as cur:
        cur.execute("SELECT anime_id, has_adaptation, last_checked_at FROM manga_adaptation_sync_state")
        return {row["anime_id"]: (row["has_adaptation"], row["last_checked_at"]) for row in cur.fetchall()}


def is_due(has_adaptation: bool | None, last_checked_at: datetime | None, now: datetime) -> bool:
    if last_checked_at is None:
        return True
    interval = RECHECK_INTERVAL_MATCHED if has_adaptation else RECHECK_INTERVAL_NO_MATCH
    return now - last_checked_at >= interval


def compute_due_anime_ids(anime_ids: list[int], state: dict[int, tuple], now: datetime) -> list[int]:
    due = []
    for anime_id in anime_ids:
        entry = state.get(anime_id)
        if entry is None or is_due(entry[0], entry[1], now):
            due.append(anime_id)
    return due


def save_sync_state(conn, anime_id: int, has_adaptation: bool, now: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO manga_adaptation_sync_state (anime_id, has_adaptation, last_checked_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (anime_id) DO UPDATE SET
                has_adaptation = EXCLUDED.has_adaptation,
                last_checked_at = EXCLUDED.last_checked_at
            """,
            (anime_id, has_adaptation, now),
        )


def save_adaptation(conn, anime_id: int, source_type: str, row: dict, now: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO manga_adaptation_cache (
                anime_id, source_type, anilist_source_id, title, status,
                latest_chapter, latest_volume, last_release_at,
                licensor_name, licensor_url, cover_image_url,
                mangadex_id, mangaupdates_id, match_method, synced_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (anime_id, source_type) DO UPDATE SET
                anilist_source_id = EXCLUDED.anilist_source_id,
                title = EXCLUDED.title,
                status = EXCLUDED.status,
                latest_chapter = EXCLUDED.latest_chapter,
                latest_volume = EXCLUDED.latest_volume,
                last_release_at = EXCLUDED.last_release_at,
                licensor_name = EXCLUDED.licensor_name,
                licensor_url = EXCLUDED.licensor_url,
                cover_image_url = EXCLUDED.cover_image_url,
                mangadex_id = EXCLUDED.mangadex_id,
                mangaupdates_id = EXCLUDED.mangaupdates_id,
                match_method = EXCLUDED.match_method,
                synced_at = EXCLUDED.synced_at
            """,
            (
                anime_id, source_type, row["anilist_source_id"], row["title"], row["status"],
                row.get("latest_chapter"), row.get("latest_volume"), row.get("last_release_at"),
                row.get("licensor_name"), row.get("licensor_url"), row.get("cover_image_url"),
                row.get("mangadex_id"), row.get("mangaupdates_id"), row["match_method"], now,
            ),
        )


# ── Sync logic ────────────────────────────────────────────────────────────────

def resolve_one_source(candidate_id: int, candidate_title: str) -> tuple[str, dict] | None:
    """Resolves one SOURCE candidate to a (source_type, row_dict) pair, or None
    if its real AniList format isn't one this app tracks (MANGA/NOVEL/ONE_SHOT
    only — a SOURCE edge can point at other media types this app has no use
    for, e.g. a visual novel)."""
    metadata = fetch_source_metadata(candidate_id)
    time.sleep(ANILIST_SLEEP)
    if metadata is None:
        return None
    fmt = metadata.get("format")
    if fmt not in SOURCE_FORMATS:
        return None
    # ONE_SHOT is a single-volume manga, not prose — tracked under MANGA, not NOVEL.
    source_type = "NOVEL" if fmt == "NOVEL" else "MANGA"

    external_links = metadata.get("externalLinks") or []
    licensor_name, licensor_url = english_licensor_from_links(external_links)

    row = {
        "anilist_source_id": candidate_id,
        "title": candidate_title,
        "status": metadata.get("status"),
        "latest_chapter": metadata.get("chapters"),
        "latest_volume": metadata.get("volumes"),
        "last_release_at": None,
        "licensor_name": licensor_name,
        "licensor_url": licensor_url,
        "cover_image_url": None,
        "mangadex_id": None,
        "mangaupdates_id": None,
        "match_method": "anilist_only",
    }

    try:
        mangadex_hit = resolve_mangadex_match(candidate_title, candidate_id)
        time.sleep(MANGADEX_SLEEP)
        if mangadex_hit:
            row["mangadex_id"] = mangadex_hit["id"]
            mu_series_id = resolve_mangaupdates_series_id(candidate_title, mangadex_hit.get("mu_slug"))
            time.sleep(MANGAUPDATES_SLEEP)
            if mu_series_id:
                series = fetch_mangaupdates_series(mu_series_id)
                if series:
                    row["mangaupdates_id"] = str(mu_series_id)
                    row["latest_chapter"] = series.get("latest_chapter") or row["latest_chapter"]
                    last_updated = series.get("last_updated") or {}
                    row["last_release_at"] = last_updated.get("as_rfc3339")
                    mu_licensor = english_publisher_from_mangaupdates(series)
                    if mu_licensor:
                        row["licensor_name"] = mu_licensor
                    row["match_method"] = "mangadex_verified"
    except Exception as e:
        print(f"    WARNING: MangaDex/MangaUpdates lookup failed for '{candidate_title}': {e}", file=sys.stderr)

    return source_type, row


def sync_one_anime(conn, anime_id: int, now: datetime) -> bool:
    """Checks one anime for a manga/novel source, upserts whatever's found, and
    always records sync state. Returns whether any adaptation was found."""
    candidates = fetch_source_candidates(anime_id)
    time.sleep(ANILIST_SLEEP)

    found_any = False
    seen_types: set[str] = set()
    for candidate in candidates:
        resolved = resolve_one_source(candidate["id"], candidate["title"])
        if resolved is None:
            continue
        source_type, row = resolved
        if source_type in seen_types:
            # Multiple SOURCE edges of the same type (e.g. two manga adaptations
            # of unrelated source material) — keep the first, matching
            # manga_adaptation_cache's UNIQUE (anime_id, source_type) constraint.
            continue
        seen_types.add(source_type)
        save_adaptation(conn, anime_id, source_type, row, now)
        found_any = True

    save_sync_state(conn, anime_id, found_any, now)
    conn.commit()
    return found_any


def main() -> None:
    conn = db_connect()
    try:
        anime_ids = fetch_catalog_anime_ids(conn)
        if not anime_ids:
            print("No anime in local catalog — skipping.")
            return

        now = datetime.now(timezone.utc)
        state = load_sync_state(conn)
        due_ids = compute_due_anime_ids(anime_ids, state, now)
        print(
            f"{len(due_ids)} of {len(anime_ids)} catalog anime due for a manga/LN check.",
            flush=True,
        )

        checked = matched = 0
        for anime_id in due_ids:
            try:
                if sync_one_anime(conn, anime_id, now):
                    matched += 1
                checked += 1
            except Exception as e:
                conn.rollback()
                print(f"  ERROR checking anime_id={anime_id}: {e}", file=sys.stderr)

        print(f"Done. Checked {checked}/{len(due_ids)} due titles, {matched} had a matched source.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
