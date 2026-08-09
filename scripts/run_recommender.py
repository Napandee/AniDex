#!/usr/bin/env python3
"""
Standalone recommender script.

Builds a taste profile from the user's library (completed/watching/planning),
fetches candidate anime via AniList's recommendations API, scores them, and
upserts results into recommendation_scores. Re-runnable and idempotent.
The `dismissed` flag on existing rows is never touched by this script.

Usage:
    python scripts/run_recommender.py
"""

import json
import os
import sys
import time

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

ANILIST_API = "https://graphql.anilist.co"
DATABASE_URL = os.environ["DATABASE_URL"]
ANILIST_TOKEN = os.environ.get("ANILIST_TOKEN")

TOP_SHOWS_FOR_RECS = 30   # how many of the user's top completed shows to pull recs for
RECS_PER_SHOW_PAGES = 2   # pages of recommendations per show (25 per page = up to 50 recs/show)
INTER_REQUEST_SLEEP = 0.8  # seconds between API calls

# Contribution weight per status when building the taste profile.
# COMPLETED entries are weighted by their score (score/5); these are the fallbacks.
COMPLETED_UNSCORED_WEIGHT = 0.5
WATCHING_WEIGHT = 0.4
PLANNING_WEIGHT = 0.3

RECOMMENDATIONS_QUERY = """
query ($mediaId: Int, $page: Int) {
  Media(id: $mediaId) {
    recommendations(page: $page, perPage: 25, sort: RATING_DESC) {
      pageInfo { hasNextPage }
      nodes {
        rating
        mediaRecommendation {
          id
          type
          title { romaji }
        }
      }
    }
  }
}
"""

MEDIA_DETAILS_QUERY = """
query ($ids: [Int], $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage currentPage }
    media(id_in: $ids, type: ANIME) {
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
      description(asHtml: false)
      externalLinks { site url }
      streamingEpisodes { title url site thumbnail }
    }
  }
}
"""


# ---------------------------------------------------------------------------
# AniList API
# ---------------------------------------------------------------------------

def gql(query: str, variables: dict, retries: int = 5) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if ANILIST_TOKEN:
        headers["Authorization"] = f"Bearer {ANILIST_TOKEN}"
    for _ in range(retries):
        resp = httpx.post(
            ANILIST_API,
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 60))
            print(f"  Rate limited — waiting {wait}s...", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        payload = resp.json()
        if "errors" in payload:
            raise RuntimeError(f"AniList error: {payload['errors']}")
        return payload["data"]
    raise RuntimeError("Still rate-limited after retries")


# ---------------------------------------------------------------------------
# Taste profile
# ---------------------------------------------------------------------------

def build_taste_profile(conn) -> dict:
    """
    Build weighted genre / tag / studio vectors from the user's library.

    COMPLETED entries are weighted by normalised score (score/5).
    Unscored completed entries get a neutral 0.5 weight so they still
    contribute even while scoring is in progress.
    WATCHING and PLANNING contribute at fixed weights to keep the profile
    broad — the user declared interest in these even without a rating.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT a.genres, a.tags, a.studios, le.status, le.score
            FROM library_entries le
            JOIN anime a ON a.id = le.anime_id
            WHERE le.status IN ('COMPLETED', 'WATCHING', 'PLANNING')
        """)
        rows = cur.fetchall()

    genre_weights: dict[str, float] = {}
    tag_weights: dict[str, float] = {}
    studio_weights: dict[str, float] = {}

    for row in rows:
        status = row["status"]
        score = row["score"]

        if status == "COMPLETED":
            weight = float(score) / 5.0 if score else COMPLETED_UNSCORED_WEIGHT
        elif status == "WATCHING":
            weight = WATCHING_WEIGHT
        else:  # PLANNING
            weight = PLANNING_WEIGHT

        for genre in (row["genres"] or []):
            genre_weights[genre] = genre_weights.get(genre, 0.0) + weight

        for tag in (row["tags"] or []):
            tag_weights[tag["name"]] = (
                tag_weights.get(tag["name"], 0.0) + weight * (tag["rank"] / 100.0)
            )

        for studio in (row["studios"] or []):
            if studio.get("isMain"):
                studio_weights[studio["name"]] = (
                    studio_weights.get(studio["name"], 0.0) + weight
                )

    return {"genres": genre_weights, "tags": tag_weights, "studios": studio_weights}


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------

def get_library_ids(conn) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT anime_id FROM library_entries")
        return {row[0] for row in cur.fetchall()}


def get_top_completed_ids(conn, limit: int) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT anime_id FROM library_entries
            WHERE status = 'COMPLETED'
            ORDER BY score DESC NULLS LAST, synced_at DESC
            LIMIT %s
        """, (limit,))
        return [row[0] for row in cur.fetchall()]


def get_planning_ids(conn) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT anime_id FROM library_entries WHERE status = 'PLANNING'")
        return {row[0] for row in cur.fetchall()}


def fetch_recommendation_candidates(
    top_show_ids: list[int],
    library_ids: set[int],
) -> set[int]:
    """
    For each of the user's top completed shows, pull AniList community
    recommendations and collect anime IDs not already in the library.
    """
    candidates: set[int] = set()
    for i, media_id in enumerate(top_show_ids, 1):
        print(f"  [{i}/{len(top_show_ids)}] Fetching recs for media id {media_id}...", flush=True)
        for page in range(1, RECS_PER_SHOW_PAGES + 1):
            data = gql(RECOMMENDATIONS_QUERY, {"mediaId": media_id, "page": page})
            recs = data["Media"]["recommendations"]
            for node in recs["nodes"]:
                rec = node.get("mediaRecommendation")
                if rec and rec["type"] == "ANIME" and rec["id"] not in library_ids:
                    candidates.add(rec["id"])
            if not recs["pageInfo"]["hasNextPage"]:
                break
            time.sleep(INTER_REQUEST_SLEEP)
        time.sleep(INTER_REQUEST_SLEEP)
    return candidates


def fetch_and_store_candidates(conn, media_ids: list[int]) -> None:
    """Batch-fetch media details from AniList and upsert into the anime table."""
    remaining = list(media_ids)
    batch_num = 0
    while remaining:
        batch_num += 1
        batch = remaining[:50]
        remaining = remaining[50:]
        print(f"  Batch {batch_num}: fetching details for {len(batch)} anime...", flush=True)
        data = gql(MEDIA_DETAILS_QUERY, {"ids": batch, "page": 1})
        with conn.cursor() as cur:
            for media in data["Page"]["media"]:
                _upsert_anime(cur, media)
        conn.commit()
        if remaining:
            time.sleep(INTER_REQUEST_SLEEP)


def _upsert_anime(cur, media: dict) -> None:
    studios = [
        {"name": e["node"]["name"], "isMain": e["isMain"]}
        for e in (media.get("studios") or {}).get("edges", [])
    ]
    tags = [{"name": t["name"], "rank": t["rank"]} for t in (media.get("tags") or [])]
    ext_links = [{"site": l["site"], "url": l["url"]} for l in (media.get("externalLinks") or [])]
    streaming = [
        {"title": s["title"], "url": s["url"], "site": s["site"], "thumbnail": s.get("thumbnail")}
        for s in (media.get("streamingEpisodes") or [])
    ]
    cur.execute("""
        INSERT INTO anime (
            id, id_mal, title_romaji, title_english, title_native,
            format, status, episodes, season, season_year,
            genres, tags, studios, average_score,
            cover_image_url, banner_image_url, description,
            external_links, streaming_episodes, last_synced_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
        ON CONFLICT (id) DO UPDATE SET
            title_english      = EXCLUDED.title_english,
            genres             = EXCLUDED.genres,
            tags               = EXCLUDED.tags,
            studios            = EXCLUDED.studios,
            average_score      = EXCLUDED.average_score,
            cover_image_url    = EXCLUDED.cover_image_url,
            banner_image_url   = EXCLUDED.banner_image_url,
            last_synced_at     = now()
    """, (
        media["id"], media.get("idMal"),
        media["title"]["romaji"], media["title"].get("english"), media["title"].get("native"),
        media.get("format"), media.get("status"), media.get("episodes"),
        media.get("season"), media.get("seasonYear"),
        json.dumps(media.get("genres") or []),
        json.dumps(tags), json.dumps(studios),
        media.get("averageScore"),
        (media.get("coverImage") or {}).get("large"),
        media.get("bannerImage"), media.get("description"),
        json.dumps(ext_links), json.dumps(streaming),
    ))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_candidate(media: dict, profile: dict) -> tuple[float, dict]:
    genres = media.get("genres") or []
    tags = media.get("tags") or []
    studios = media.get("studios") or []

    # Genre overlap
    genre_hits = [(g, profile["genres"][g]) for g in genres if g in profile["genres"]]
    genre_score = sum(w for _, w in genre_hits) / max(len(genres), 1)

    # Tag overlap (scaled by AniList's tag relevance rank for that show)
    tag_hits = [
        (t["name"], profile["tags"][t["name"]] * (t["rank"] / 100.0))
        for t in tags
        if t["name"] in profile["tags"]
    ]
    tag_score = sum(w for _, w in tag_hits) / max(len(tags), 1)

    # Studio match — only main studios count
    main_studios = [s["name"] for s in studios if s.get("isMain") and s["name"] in profile["studios"]]
    studio_score = max((profile["studios"][s] for s in main_studios), default=0.0)

    raw = genre_score * 0.5 + tag_score * 0.3 + studio_score * 0.2

    reason = {
        "matched_genres": [g for g, _ in sorted(genre_hits, key=lambda x: -x[1])[:5]],
        "matched_tags":   [t for t, _ in sorted(tag_hits,   key=lambda x: -x[1])[:5]],
        "matched_studio": main_studios[0] if main_studios else None,
    }
    return raw, reason


def score_and_store(conn, all_candidate_ids: set[int], profile: dict) -> int:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, genres, tags, studios
            FROM anime WHERE id = ANY(%s)
        """, (list(all_candidate_ids),))
        media_rows = {row["id"]: dict(row) for row in cur.fetchall()}

    scored: list[tuple[int, float, dict]] = []
    for anime_id, media in media_rows.items():
        raw, reason = score_candidate(media, profile)
        scored.append((anime_id, raw, reason))

    # Normalise so the best candidate always hits 100
    if scored:
        max_raw = max(s[1] for s in scored) or 1.0
        scored = [(aid, (raw / max_raw) * 100.0, reason) for aid, raw, reason in scored]

    with conn.cursor() as cur:
        for anime_id, score, reason in scored:
            cur.execute("""
                INSERT INTO recommendation_scores (anime_id, score, reason, dismissed, computed_at)
                VALUES (%s, %s, %s, false, now())
                ON CONFLICT (anime_id) DO UPDATE SET
                    score       = EXCLUDED.score,
                    reason      = EXCLUDED.reason,
                    computed_at = now()
                    -- dismissed is intentionally excluded: user decisions survive re-runs
            """, (anime_id, score, json.dumps(reason)))
    conn.commit()
    return len(scored)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    conn = psycopg2.connect(DATABASE_URL)
    try:
        print("Step 1/4 — Building taste profile from library...")
        profile = build_taste_profile(conn)
        print(
            f"  Profile: {len(profile['genres'])} genres, "
            f"{len(profile['tags'])} tags, "
            f"{len(profile['studios'])} studios"
        )

        library_ids = get_library_ids(conn)
        top_ids = get_top_completed_ids(conn, TOP_SHOWS_FOR_RECS)
        planning_ids = get_planning_ids(conn)
        print(
            f"  Library: {len(library_ids)} shows total | "
            f"top {len(top_ids)} completed used for recs | "
            f"{len(planning_ids)} PLANNING entries as direct candidates"
        )

        print(f"\nStep 2/4 — Fetching AniList recommendations for top {len(top_ids)} shows...")
        rec_candidate_ids = fetch_recommendation_candidates(top_ids, library_ids)
        print(f"  {len(rec_candidate_ids)} unique external candidates discovered")

        all_candidate_ids = rec_candidate_ids | planning_ids
        print(f"  {len(all_candidate_ids)} total candidates to score")

        # Fetch details for candidates not yet in our anime table
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM anime WHERE id = ANY(%s)", (list(all_candidate_ids),))
            known_ids = {row[0] for row in cur.fetchall()}
        unknown_ids = all_candidate_ids - known_ids
        if unknown_ids:
            print(f"\nStep 3/4 — Fetching details for {len(unknown_ids)} new anime...")
            fetch_and_store_candidates(conn, list(unknown_ids))
        else:
            print("\nStep 3/4 — All candidate details already cached, skipping fetch.")

        print(f"\nStep 4/4 — Scoring and storing {len(all_candidate_ids)} candidates...")
        n = score_and_store(conn, all_candidate_ids, profile)
        print(f"\nDone — {n} recommendations scored and stored.")

    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
