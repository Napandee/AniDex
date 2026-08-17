#!/usr/bin/env python3
"""
Standalone AniList sync script — single-user, invoked once per user by
run_full_sync.py (which sets USER_ID) or directly for local dev/testing.

Pulls the full library for the configured AniList user and upserts into the
anime (global) + library_entries (this user only) tables. Idempotent — safe
to re-run at any time.

Usage:
    USER_ID=1 python scripts/sync_anilist.py

Requires .env (or env vars) with:
    DATABASE_URL, ANILIST_USERNAME, ANILIST_TOKEN, USER_ID
"""

import json
import math
import os
import sys
import time
from datetime import date, datetime, timezone

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

ANILIST_API = "https://graphql.anilist.co"
DATABASE_URL = os.environ["DATABASE_URL"]
ANILIST_USERNAME = os.environ["ANILIST_USERNAME"]
ANILIST_TOKEN = os.environ.get("ANILIST_TOKEN")
USER_ID = int(os.environ["USER_ID"])

# Fetch all list entries in chunks; AniList max is 500 per chunk.
# Each chunk = 1 API call; we sleep between chunks to stay under 90 req/min.
PER_CHUNK = 500
INTER_CHUNK_SLEEP = 0.8  # seconds

LIBRARY_QUERY = """
query ($userName: String, $chunk: Int, $perChunk: Int) {
  MediaListCollection(userName: $userName, type: ANIME, chunk: $chunk, perChunk: $perChunk) {
    hasNextChunk
    lists {
      entries {
        id
        status
        score(format: POINT_5)
        progress
        repeat
        startedAt  { year month day }
        completedAt { year month day }
        updatedAt
        media {
          id
          idMal
          title { romaji english native }
          format
          status
          episodes
          season
          seasonYear
          genres
          tags { name rank }
          studios { edges { isMain node { name } } }
          averageScore
          coverImage { large }
          bannerImage
          duration
          description(asHtml: false)
          trailer { id site }
          externalLinks { site url }
          streamingEpisodes { title url site thumbnail }
          relations {
            edges {
              relationType
              node {
                id
                title { romaji english }
                coverImage { large }
                format
              }
            }
          }
        }
      }
    }
  }
}
"""


def gql(query: str, variables: dict, retries: int = 5) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if ANILIST_TOKEN:
        headers["Authorization"] = f"Bearer {ANILIST_TOKEN}"

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


def fetch_all_entries() -> list:
    entries = []
    chunk = 1
    while True:
        print(f"  Fetching chunk {chunk}...", flush=True)
        data = gql(LIBRARY_QUERY, {
            "userName": ANILIST_USERNAME,
            "chunk": chunk,
            "perChunk": PER_CHUNK,
        })
        collection = data["MediaListCollection"]
        for lst in collection["lists"]:
            entries.extend(lst["entries"])
        if not collection["hasNextChunk"]:
            break
        chunk += 1
        time.sleep(INTER_CHUNK_SLEEP)
    return entries


def parse_date(d: dict | None) -> date | None:
    if not d or not d.get("year"):
        return None
    try:
        return date(d["year"], d["month"] or 1, d["day"] or 1)
    except (ValueError, TypeError):
        return None


def score_to_stars(score_5: float | None) -> int | None:
    """Return AniList POINT_5 score (1–5) as an integer, or None if unrated."""
    if not score_5:
        return None
    return int(score_5)


def upsert_anime(cur, media: dict) -> None:
    studios = [
        {"name": e["node"]["name"], "isMain": e["isMain"]}
        for e in (media.get("studios") or {}).get("edges", [])
    ]
    tags = [
        {"name": t["name"], "rank": t["rank"]}
        for t in (media.get("tags") or [])
    ]
    ext_links = [
        {"site": lnk["site"], "url": lnk["url"]}
        for lnk in (media.get("externalLinks") or [])
    ]
    streaming = [
        {
            "title": ep["title"],
            "url": ep["url"],
            "site": ep["site"],
            "thumbnail": ep.get("thumbnail"),
        }
        for ep in (media.get("streamingEpisodes") or [])
    ]
    trailer_raw = media.get("trailer") or {}
    trailer_yt_id = (
        trailer_raw.get("id")
        if trailer_raw.get("site", "").lower() == "youtube"
        else None
    )

    relations = [
        {
            "id": edge["node"]["id"],
            "title": (edge["node"].get("title") or {}).get("english")
                     or (edge["node"].get("title") or {}).get("romaji", ""),
            "cover": (edge["node"].get("coverImage") or {}).get("large"),
            "format": edge["node"].get("format"),
            "relation_type": edge.get("relationType", "OTHER"),
        }
        for edge in ((media.get("relations") or {}).get("edges") or [])
        if edge.get("node")
    ]

    cur.execute(
        """
        INSERT INTO anime (
            id, id_mal, title_romaji, title_english, title_native,
            format, status, episodes, duration, season, season_year,
            genres, tags, studios, average_score,
            cover_image_url, banner_image_url, description,
            trailer_yt_id, external_links, streaming_episodes, relations, last_synced_at
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s, now()
        )
        ON CONFLICT (id) DO UPDATE SET
            id_mal             = EXCLUDED.id_mal,
            title_romaji       = EXCLUDED.title_romaji,
            title_english      = EXCLUDED.title_english,
            title_native       = EXCLUDED.title_native,
            format             = EXCLUDED.format,
            status             = EXCLUDED.status,
            episodes           = EXCLUDED.episodes,
            duration           = EXCLUDED.duration,
            season             = EXCLUDED.season,
            season_year        = EXCLUDED.season_year,
            genres             = EXCLUDED.genres,
            tags               = EXCLUDED.tags,
            studios            = EXCLUDED.studios,
            average_score      = EXCLUDED.average_score,
            cover_image_url    = EXCLUDED.cover_image_url,
            banner_image_url   = EXCLUDED.banner_image_url,
            description        = EXCLUDED.description,
            trailer_yt_id      = EXCLUDED.trailer_yt_id,
            external_links     = EXCLUDED.external_links,
            streaming_episodes = EXCLUDED.streaming_episodes,
            relations          = EXCLUDED.relations,
            last_synced_at     = now()
        """,
        (
            media["id"],
            media.get("idMal"),
            media["title"]["romaji"],
            media["title"].get("english"),
            media["title"].get("native"),
            media.get("format"),
            media.get("status"),
            media.get("episodes"),
            media.get("duration"),
            media.get("season"),
            media.get("seasonYear"),
            json.dumps(media.get("genres") or []),
            json.dumps(tags),
            json.dumps(studios),
            media.get("averageScore"),
            (media.get("coverImage") or {}).get("large"),
            media.get("bannerImage"),
            media.get("description"),
            trailer_yt_id,
            json.dumps(ext_links),
            json.dumps(streaming),
            json.dumps(relations),
        ),
    )


STATUS_MAP = {"CURRENT": "WATCHING"}


def upsert_library_entry(cur, entry: dict) -> None:
    media_id = entry["media"]["id"]
    score = score_to_stars(entry.get("score"))
    status = STATUS_MAP.get(entry["status"], entry["status"])

    updated_ts = entry.get("updatedAt")
    anilist_updated_at = (
        datetime.fromtimestamp(updated_ts, tz=timezone.utc) if updated_ts else None
    )

    cur.execute(
        """
        INSERT INTO library_entries (
            user_id, anime_id, anilist_entry_id, status, score, progress,
            repeat_count, start_date, finish_date, anilist_updated_at, synced_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (user_id, anime_id) DO UPDATE SET
            anilist_entry_id   = EXCLUDED.anilist_entry_id,
            status             = EXCLUDED.status,
            score              = COALESCE(EXCLUDED.score, library_entries.score),
            progress           = EXCLUDED.progress,
            repeat_count       = EXCLUDED.repeat_count,
            start_date         = EXCLUDED.start_date,
            finish_date        = EXCLUDED.finish_date,
            anilist_updated_at = EXCLUDED.anilist_updated_at,
            synced_at          = now()
        WHERE library_entries.sync_status = 'synced'
        """,
        (
            USER_ID,
            media_id,
            entry["id"],
            status,
            score,
            entry.get("progress", 0),
            entry.get("repeat", 0),
            parse_date(entry.get("startedAt")),
            parse_date(entry.get("completedAt")),
            anilist_updated_at,
        ),
    )


def main() -> None:
    print(f"Syncing AniList library for: {ANILIST_USERNAME} (user_id={USER_ID})")
    print(f"  Token present: {'yes' if ANILIST_TOKEN else 'NO — private lists will be skipped'}")

    try:
        entries = fetch_all_entries()
    except Exception as e:
        print(f"ERROR fetching from AniList: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  {len(entries)} list entries fetched")

    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                for i, entry in enumerate(entries, 1):
                    upsert_anime(cur, entry["media"])
                    upsert_library_entry(cur, entry)
                    if i % 100 == 0:
                        print(f"  {i}/{len(entries)} upserted...", flush=True)
        print(f"Done — {len(entries)} entries synced successfully.")
    except Exception as e:
        print(f"ERROR writing to database: {e}", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
