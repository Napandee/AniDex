#!/usr/bin/env python3
"""
Standalone global job — syncs filler/canon episode status from AniFillerPedia
(github.com/Napandee/AniFillerPedia) into filler_episode_cache, one pass over
every distinct anime.id already in the local catalog (issue #299). Foundational
data layer for the three filler-UI concepts filed alongside it (#300/#301/#302) —
they read filler_episode_cache/filler_sync_state/filler_data_license, they never
talk to AniFillerPedia directly.

AniFillerPedia keys its own `series` table by anilist_id — the exact same id
anime.id already is, so this is a direct integer lookup, no fuzzy title matching.
Catalog-wide like scripts/sync_airing_schedule.py, not per-user: filler/canon
status doesn't depend on who's watching or what their library status is. Unlike
airing-schedule data, filler status barely changes once approved, so this runs on
its own low-frequency (daily) schedule — see app/main.py's _apply_schedule — not
tied to the hourly airing-schedule cadence.

Coverage will be sparse for a long time — AniFillerPedia's own README describes
its dataset as still early-stage ("initial cluster of well-known long-running
shows"). A title with no series match, or a matched series with zero researched
episodes, is a normal, expected outcome, not an error — both cases are recorded in
filler_sync_state (so a later run knows not to re-query the same title on every
single pass forever) with no rows written to filler_episode_cache.

Note for a future maintainer: AniList sometimes splits a long-running show into
separate per-season media entries (e.g. Kingdom S1-S4, issue #266). If a
split-season show turns up matched in AniFillerPedia's dataset, verify its
episode-number alignment against AniList's own per-media episode count before
trusting it blindly for that title — see issue #161's final body and #299's own
"open questions". Nothing here currently detects or corrects for that; it's a
manual-verification note, not (yet) handled in code.

Usage:
    python scripts/sync_filler_data.py

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

AFP_API = "https://anifillerpedia.wiki/api/v1"
DATABASE_URL = os.environ["DATABASE_URL"]

# Polite inter-request pause — same order of magnitude as
# sync_airing_schedule.py/anilist_sync_common.py use against AniList. AniFillerPedia's
# own docs describe no rate-limit wall for reasonable use; this is just good manners
# given this app's catalog can run to hundreds of titles.
REQUEST_SLEEP = 0.5

# Re-check cadences (issue #299's "open questions"): a matched series with thin/no
# researched episodes yet is more likely to gain real research soon than an unmatched
# title is to suddenly gain a brand new community-proposed series entry, so an
# unmatched title gets a longer cooldown than a matched-but-thin one.
RECHECK_INTERVAL_MATCHED = timedelta(days=14)
RECHECK_INTERVAL_NO_MATCH = timedelta(days=60)


def http_get(path: str, params: dict | None = None, retries: int = 3) -> httpx.Response:
    url = f"{AFP_API}{path}"
    for attempt in range(retries):
        resp = httpx.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 30))
            print(f"  Rate limited — waiting {wait}s before retry...", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"AniFillerPedia API still rate-limiting {path} after retries")


def fetch_series_match(anilist_id: int) -> dict | None:
    """Return the matching AniFillerPedia series dict for this anilist_id, or None if
    no series exists in their catalog yet — a normal, expected outcome given their
    catalog is still being grown by community proposals, not an error. anilist_id is a
    direct key match (no fuzzy matching), so at most one item is expected; take the
    first defensively rather than assuming the list is never longer than one."""
    resp = http_get("/series", params={"anilist_id": anilist_id})
    items = resp.json().get("items", [])
    return items[0] if items else None


def fetch_episodes(series_id: int) -> list[dict]:
    """Return this series' researched episodes. An empty list means the series exists
    in AniFillerPedia's catalog but has no researched episodes yet — a normal outcome
    given their dataset is still early-stage, not an error."""
    resp = http_get(f"/series/{series_id}/episodes")
    return resp.json()


def fetch_license() -> dict:
    resp = http_get("/license")
    return resp.json()


def is_due(afp_series_id: int | None, last_checked_at: datetime | None, now: datetime) -> bool:
    """Whether a title is due for a (re-)check, per the recheck cadences above. Never
    checked before (last_checked_at is None) is always due."""
    if last_checked_at is None:
        return True
    interval = RECHECK_INTERVAL_MATCHED if afp_series_id is not None else RECHECK_INTERVAL_NO_MATCH
    return now - last_checked_at >= interval


def load_sync_state(conn) -> dict[int, tuple[int | None, datetime]]:
    """anime_id -> (afp_series_id, last_checked_at) for every title ever checked."""
    with conn.cursor() as cur:
        cur.execute("SELECT anime_id, afp_series_id, last_checked_at FROM filler_sync_state")
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def compute_due_anime_ids(anime_ids: list[int], state: dict[int, tuple], now: datetime) -> list[int]:
    """Filter the full catalog down to just the titles due for a check right now —
    the "already checked recently, don't re-query every run" guard from issue #299's
    acceptance criteria."""
    due = []
    for anime_id in anime_ids:
        entry = state.get(anime_id)
        if entry is None or is_due(entry[0], entry[1], now):
            due.append(anime_id)
    return due


def fetch_catalog_anime_ids(conn) -> list[int]:
    """Every distinct anime already in the local catalog — the global `anime` table,
    not scoped to any one user's library, matching issue #299's "every anime in
    AniDex's catalog" scope (unlike sync_airing_schedule.py, which narrows to
    currently-relevant WATCHING/PLANNING RELEASING titles — filler status is static
    catalog metadata, not something tied to airing state)."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM anime ORDER BY id")
        return [row[0] for row in cur.fetchall()]


def sync_one_anime(conn, anime_id: int, now: datetime) -> None:
    """Check one anime against AniFillerPedia and record the outcome. Always writes a
    filler_sync_state row (a checked-but-unmatched title still needs its
    last_checked_at bumped so it isn't re-queried on every run). Only writes
    filler_episode_cache rows when there's an actual match with researched episodes."""
    match = fetch_series_match(anime_id)

    with conn.cursor() as cur:
        if match is None:
            cur.execute(
                """
                INSERT INTO filler_sync_state (anime_id, afp_series_id, last_checked_at)
                VALUES (%s, NULL, %s)
                ON CONFLICT (anime_id) DO UPDATE SET
                    afp_series_id = NULL,
                    last_checked_at = EXCLUDED.last_checked_at
                """,
                (anime_id, now),
            )
            conn.commit()
            return

        series_id = match["id"]
        episodes = fetch_episodes(series_id)

        # Delete-then-reinsert scoped to this one anime_id, mirroring
        # sync_airing_schedule.py's delete+reinsert shape — handles corrections (a
        # status/citation getting fixed on AniFillerPedia's side) and shrinkage
        # (an episode's research getting retracted) cleanly without a separate diff.
        cur.execute("DELETE FROM filler_episode_cache WHERE anime_id = %s", (anime_id,))
        for ep in episodes:
            citation = ep.get("citation") or {}
            cur.execute(
                """
                INSERT INTO filler_episode_cache
                    (anime_id, episode_number, status, status_note,
                     citation_url, citation_description, synced_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (anime_id, episode_number) DO UPDATE SET
                    status = EXCLUDED.status,
                    status_note = EXCLUDED.status_note,
                    citation_url = EXCLUDED.citation_url,
                    citation_description = EXCLUDED.citation_description,
                    synced_at = EXCLUDED.synced_at
                """,
                (
                    anime_id,
                    ep["episode_number"],
                    ep["status"],
                    ep.get("status_note"),
                    citation.get("url"),
                    citation.get("description"),
                    now,
                ),
            )

        cur.execute(
            """
            INSERT INTO filler_sync_state (anime_id, afp_series_id, last_checked_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (anime_id) DO UPDATE SET
                afp_series_id = EXCLUDED.afp_series_id,
                last_checked_at = EXCLUDED.last_checked_at
            """,
            (anime_id, series_id, now),
        )
    conn.commit()


def sync_license(conn) -> None:
    """Cache GET /license so a future UI issue can render CC BY-NC-SA attribution
    without a live call per page render — per issue #299's acceptance criteria."""
    license_data = fetch_license()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO filler_data_license (id, license_name, attribution_notice, raw_response, fetched_at)
            VALUES (1, %s, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET
                license_name = EXCLUDED.license_name,
                attribution_notice = EXCLUDED.attribution_notice,
                raw_response = EXCLUDED.raw_response,
                fetched_at = EXCLUDED.fetched_at
            """,
            (
                license_data.get("license", ""),
                license_data.get("attribution_notice", ""),
                psycopg2.extras.Json(license_data),
            ),
        )
    conn.commit()


def main() -> None:
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        try:
            sync_license(conn)
        except Exception as e:
            conn.rollback()
            print(f"WARNING: could not refresh AniFillerPedia license cache: {e}", file=sys.stderr)

        anime_ids = fetch_catalog_anime_ids(conn)
        if not anime_ids:
            print("No anime in local catalog — skipping.")
            return

        now = datetime.now(timezone.utc)
        state = load_sync_state(conn)
        due_ids = compute_due_anime_ids(anime_ids, state, now)
        print(
            f"{len(due_ids)} of {len(anime_ids)} catalog anime due for an "
            "AniFillerPedia check.",
            flush=True,
        )

        checked = 0
        for anime_id in due_ids:
            try:
                sync_one_anime(conn, anime_id, now)
                checked += 1
            except Exception as e:
                conn.rollback()
                print(f"  ERROR checking anime_id={anime_id}: {e}", file=sys.stderr)
            time.sleep(REQUEST_SLEEP)

        print(f"Done. Checked {checked}/{len(due_ids)} due titles.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
