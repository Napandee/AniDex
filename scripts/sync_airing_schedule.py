#!/usr/bin/env python3
"""
Standalone global job — refreshes airing_schedule_cache once for the union of
every user's currently-WATCHING/PLANNING RELEASING anime.

airing_schedule_cache is global (shared across all users), not per-user, so
having each user's own sync independently delete+re-insert the same rows was
both redundant (two users watching the same show each re-fetch its schedule)
and a lock/deadlock risk if two users' syncs happened to overlap in time. This
replaces that per-user step with one pass over the union, computed from local
data each user's own AniList sync already wrote — no AniList call needed just
to determine which anime are in scope.

Usage:
    python scripts/sync_airing_schedule.py

Requires .env (or env vars) with: DATABASE_URL
"""

import os
import sys
import time
from datetime import datetime, timezone

import httpx
import psycopg2
from dotenv import load_dotenv

load_dotenv()

ANILIST_API = "https://graphql.anilist.co"
DATABASE_URL = os.environ["DATABASE_URL"]

AIRING_QUERY = """
query ($ids: [Int], $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    airingSchedules(mediaId_in: $ids, notYetAired: true) {
      mediaId
      episode
      airingAt
    }
  }
}
"""


def gql(query: str, variables: dict, retries: int = 5) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    for attempt in range(retries):
        resp = httpx.post(
            ANILIST_API,
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 60))
            print(f"  Rate limited — waiting {wait}s before retry...", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        payload = resp.json()
        if "errors" in payload:
            raise RuntimeError(f"AniList API error: {payload['errors']}")
        return payload["data"]

    raise RuntimeError("AniList API still rate-limiting after retries")


def fetch_releasing_ids(conn) -> list[int]:
    """Union of anime_id across every user's WATCHING/PLANNING library that's
    currently RELEASING on AniList — read from local anime/library_entries data
    each user's own sync already wrote, not a fresh AniList query."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT le.anime_id
            FROM library_entries le
            JOIN anime a ON a.id = le.anime_id
            WHERE le.status IN ('WATCHING', 'PLANNING')
              AND a.status = 'RELEASING'
            """
        )
        return [row[0] for row in cur.fetchall()]


def fetch_airing_schedules(media_ids: list[int]) -> list[dict]:
    """Return all not-yet-aired episodes for the given media IDs."""
    schedules = []
    chunk_size = 50  # AniList accepts up to ~50 IDs per airingSchedules query reliably
    for i in range(0, len(media_ids), chunk_size):
        id_chunk = media_ids[i : i + chunk_size]
        page = 1
        while True:
            data = gql(AIRING_QUERY, {"ids": id_chunk, "page": page})
            page_data = data["Page"]
            schedules.extend(page_data["airingSchedules"])
            if not page_data["pageInfo"]["hasNextPage"]:
                break
            page += 1
            time.sleep(0.5)
        time.sleep(0.5)
    return schedules


def main() -> None:
    conn = psycopg2.connect(DATABASE_URL)
    try:
        releasing_ids = fetch_releasing_ids(conn)
        if not releasing_ids:
            print("No releasing anime across any user's library — skipping.")
            return

        print(f"Fetching airing schedules for {len(releasing_ids)} releasing anime (all users)...", flush=True)
        try:
            schedules = fetch_airing_schedules(releasing_ids)
        except Exception as e:
            print(f"ERROR fetching airing schedules: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"{len(schedules)} upcoming episodes found.")

        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM airing_schedule_cache WHERE anime_id = ANY(%s)",
                    (releasing_ids,),
                )
                for s in schedules:
                    airing_at = datetime.fromtimestamp(s["airingAt"], tz=timezone.utc)
                    cur.execute(
                        """
                        INSERT INTO airing_schedule_cache (anime_id, episode, airing_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (anime_id, episode) DO UPDATE SET
                            airing_at = EXCLUDED.airing_at
                        """,
                        (s["mediaId"], s["episode"], airing_at),
                    )
        print("Done.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
