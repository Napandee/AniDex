#!/usr/bin/env python3
"""
Standalone recommender script.

Builds a taste profile from the user's library (completed/watching/planning),
fetches candidate anime via AniList's recommendations API, scores them, and
upserts results into recommendation_scores. Re-runnable and idempotent.
The `dismissed` flag on existing rows is never touched by this script.

Single-user, invoked once per user by app/main.py's _scheduled_recommender() (which
sets USER_ID) or directly for local dev/testing.

Each run writes a `sync_log` row (type='recommender') via _start_log/_finish_log below
— issue #84, giving this job the same run-history/failure-alerting visibility that
run_full_sync.py already has for sync (issue #11).

Usage:
    USER_ID=1 python scripts/run_recommender.py
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
USER_ID = int(os.environ["USER_ID"])

TOP_SHOWS_FOR_RECS = 30   # how many of the user's top completed shows to pull recs for
RECS_PER_SHOW_PAGES = 2   # pages of recommendations per show (25 per page = up to 50 recs/show)
INTER_REQUEST_SLEEP = 0.8  # seconds between API calls

# Collaborative-filtering signal (#27) — how much other same-instance users'
# ratings move a candidate's score, tunable independently of the taste-profile
# weights below. A rating counts as "highly rated" above CROSS_USER_MIN_SCORE
# (library_entries.score is 0-5, half-star precision); CROSS_USER_SATURATION
# qualifying raters is enough to max out the signal — for a small invite-only
# instance, 3+ people loving something is already a strong signal, no need to
# keep scaling past that.
CROSS_USER_WEIGHT = 0.2
CROSS_USER_MIN_SCORE = 3.0
CROSS_USER_SATURATION = 3

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
            WHERE le.status IN ('COMPLETED', 'WATCHING', 'PLANNING') AND le.user_id = %s
        """, (USER_ID,))
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
        cur.execute("SELECT anime_id FROM library_entries WHERE user_id = %s", (USER_ID,))
        return {row[0] for row in cur.fetchall()}


def get_top_completed_ids(conn, limit: int) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT anime_id FROM library_entries
            WHERE status = 'COMPLETED' AND user_id = %s
            ORDER BY score DESC NULLS LAST, synced_at DESC
            LIMIT %s
        """, (USER_ID, limit))
        return [row[0] for row in cur.fetchall()]


def get_planning_ids(conn) -> set[int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT anime_id FROM library_entries WHERE status = 'PLANNING' AND user_id = %s",
            (USER_ID,),
        )
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
# Cross-user signal (#27) — depends on #26's privacy controls (app/privacy.py).
# Duplicated here rather than imported: scripts are standalone processes with
# their own psycopg2 connection and no shared code with the app (see
# sync_anilist.py's own gql() for the same pattern) — keep in sync with
# app/privacy.py's get_hidden_tags()/entry_hidden() if that logic ever changes.
# ---------------------------------------------------------------------------

def _hidden_tags_by_user(conn) -> dict[int, set[str]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT value FROM instance_config WHERE key = 'default_hidden_tags'")
        row = cur.fetchone()
        instance_default = json.loads(row["value"]) if row and row["value"] else []

        cur.execute("SELECT id FROM users")
        all_user_ids = {r["id"] for r in cur.fetchall()}

        cur.execute("SELECT user_id, value FROM settings WHERE key = 'hidden_tags'")
        per_user = {r["user_id"]: (json.loads(r["value"]) if r["value"] else []) for r in cur.fetchall()}

    return {
        uid: {t.strip().lower() for t in (instance_default + per_user.get(uid, [])) if t.strip()}
        for uid in all_user_ids
    }


def fetch_cross_user_signal(conn, candidate_ids: set[int], profile_owner_id: int) -> dict[int, dict]:
    """For each candidate anime, count OTHER users' ratings above
    CROSS_USER_MIN_SCORE, excluding any entry its owning rater has hidden via
    #26's tag-hiding. Returns {anime_id: {"count": int, "min_score": float}}
    for candidates with at least one qualifying, non-hidden rating."""
    hidden_by_user = _hidden_tags_by_user(conn)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT le.anime_id, le.user_id, a.genres, pn.personal_tags
            FROM library_entries le
            JOIN anime a ON a.id = le.anime_id
            LEFT JOIN personal_notes pn ON pn.anime_id = le.anime_id AND pn.user_id = le.user_id
            WHERE le.anime_id = ANY(%s) AND le.user_id != %s AND le.score > %s
            """,
            (list(candidate_ids), profile_owner_id, CROSS_USER_MIN_SCORE),
        )
        rows = cur.fetchall()

    counts: dict[int, int] = {}
    for r in rows:
        hidden = hidden_by_user.get(r["user_id"], set())
        entry_tags = {t.strip().lower() for t in (r["genres"] or []) + (r["personal_tags"] or [])}
        if entry_tags & hidden:
            continue
        counts[r["anime_id"]] = counts.get(r["anime_id"], 0) + 1

    return {aid: {"count": c, "min_score": CROSS_USER_MIN_SCORE} for aid, c in counts.items()}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_candidate(media: dict, profile: dict, cross_user: dict | None = None) -> tuple[float, dict]:
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

    # Collaborative-filtering signal (#27) — other same-instance users' ratings,
    # already privacy-filtered by fetch_cross_user_signal(). Weighted lower than
    # the user's own taste profile (genre+tag = 0.65) so it nudges rather than
    # overrides what someone's own history says they like.
    cross_user = cross_user or {}
    cross_user_count = cross_user.get("count", 0)
    cross_user_score = min(cross_user_count / CROSS_USER_SATURATION, 1.0)

    raw = (
        genre_score * 0.4
        + tag_score * 0.25
        + studio_score * 0.15
        + cross_user_score * CROSS_USER_WEIGHT
    )

    reason = {
        "matched_genres": [g for g, _ in sorted(genre_hits, key=lambda x: -x[1])[:5]],
        "matched_tags":   [t for t, _ in sorted(tag_hits,   key=lambda x: -x[1])[:5]],
        "matched_studio": main_studios[0] if main_studios else None,
        "cross_user_count":    cross_user_count or None,
        "cross_user_min_score": cross_user.get("min_score") if cross_user_count else None,
    }
    return raw, reason


def score_and_store(conn, all_candidate_ids: set[int], profile: dict) -> int:
    cross_user_signal = fetch_cross_user_signal(conn, all_candidate_ids, USER_ID)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, genres, tags, studios
            FROM anime WHERE id = ANY(%s)
        """, (list(all_candidate_ids),))
        media_rows = {row["id"]: dict(row) for row in cur.fetchall()}

    scored: list[tuple[int, float, dict]] = []
    for anime_id, media in media_rows.items():
        raw, reason = score_candidate(media, profile, cross_user_signal.get(anime_id))
        scored.append((anime_id, raw, reason))

    # Normalise so the best candidate always hits 100
    if scored:
        max_raw = max(s[1] for s in scored) or 1.0
        scored = [(aid, (raw / max_raw) * 100.0, reason) for aid, raw, reason in scored]

    with conn.cursor() as cur:
        for anime_id, score, reason in scored:
            cur.execute("""
                INSERT INTO recommendation_scores (user_id, anime_id, score, reason, dismissed, computed_at)
                VALUES (%s, %s, %s, %s, false, now())
                ON CONFLICT (user_id, anime_id) DO UPDATE SET
                    score       = EXCLUDED.score,
                    reason      = EXCLUDED.reason,
                    computed_at = now()
                    -- dismissed is intentionally excluded: user decisions survive re-runs
            """, (USER_ID, anime_id, score, json.dumps(reason)))
    conn.commit()
    return len(scored)


# ---------------------------------------------------------------------------
# sync_log — issue #84, parity with run_full_sync.py's _start_log/_finish_log so a
# recommender run is queryable the same way a sync run is (GET /api/sync/log already
# returns every type for the logged-in user, and the frontend already renders
# type == 'recommender' rows — see settings.html's fmtType()). No per-step `steps`
# breakdown here since this script isn't a multi-step pipeline like run_full_sync.py.
# ---------------------------------------------------------------------------

def _start_log() -> int:
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sync_log (user_id, type, status) VALUES (%s, 'recommender', 'running') "
                "RETURNING id",
                (USER_ID,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def _finish_log(log_id: int, status: str, entries_updated: int | None, error_msg: str | None) -> None:
    try:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE sync_log SET status = %s, entries_updated = %s, error_msg = %s WHERE id = %s",
                    (status, entries_updated, error_msg, log_id),
                )
        finally:
            conn.close()
    except Exception as e:
        print(f"Warning: could not finalize recommender sync log: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log_id = _start_log()
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
        _finish_log(log_id, "ok", n, None)

    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        _finish_log(log_id, "error", None, str(e)[:800])
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
