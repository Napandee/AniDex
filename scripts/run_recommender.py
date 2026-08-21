#!/usr/bin/env python3
"""
Standalone recommender script.

Builds a taste profile from the user's library (completed/watching/planning),
fetches candidate anime via AniList's recommendations API, scores them, and
upserts results into recommendation_scores. Re-runnable and idempotent.
The `dismissed` flag on existing rows is never touched by this script, and
neither is `snoozed_until` (issue #75's time-boxed "not now") — both are user
decisions that must survive a rebuild.

Two candidate-discovery paths write into the same recommendation_scores table,
distinguished by the `source` column (issue #13, migration 012):
  - 'similarity': fetch_recommendation_candidates() — AniList's per-show
    recommendations off what you've completed, plus your own PLANNING entries.
    This is the original path.
  - 'seasonal': fetch_seasonal_candidates() — AniList's current season/year
    query (issue #13's "new this season" digest: currently-airing/upcoming
    anime you haven't added yet, scored against the same taste profile). Additive,
    not a replacement — see CLAUDE.md's Scope section.
Both paths reuse the same build_taste_profile()/score_candidate() scoring logic;
the only real difference between them is candidate *sourcing*.

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
from datetime import datetime, timezone

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

# Seasonal discovery digest (issue #13) — how many pages of the current season's
# release list to pull, batched via AniList's Page pagination rather than looping
# one show at a time. 3 pages * 50/page = up to 150 candidates per run, sorted by
# AniList popularity — plenty for a single season's worth of releases without
# ballooning the scoring/storage work below.
SEASONAL_MAX_PAGES = 3

# Issue #254 — a candidate's `anime` row used to be treated as "done forever" the
# moment it existed at all, so any feature shipped later that depends on a new bit
# of media detail (e.g. #158's `relations`, needed to suppress already-owned
# sequels) silently never backfilled for candidates discovered before that feature
# existed. CANDIDATE_STALENESS_DAYS re-fetches/re-upserts a candidate's full AniList
# details if its `anime.last_synced_at` is older than this, not just when the row is
# missing outright. 14 days chosen as the balance point: the built-in scheduler
# reruns this job weekly, so every candidate still gets refreshed within
# roughly its next 1-2 runs (self-healing a gap like #254's within a bounded, short
# window) without re-fetching data on every single run for candidates that haven't
# had a chance to change — AniList's 90 req/min budget is per-run cheap either way
# (candidate sets here run in the hundreds, batched 50/request by
# fetch_and_store_candidates), so the number is chosen for staleness-tolerance, not
# because a smaller value would risk the rate limit.
CANDIDATE_STALENESS_DAYS = 14

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

SEASONAL_IDS_QUERY = """
query ($season: MediaSeason, $seasonYear: Int, $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    media(season: $season, seasonYear: $seasonYear, type: ANIME, sort: POPULARITY_DESC) {
      id
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


def get_library_statuses(conn) -> dict[int, str]:
    """anime_id -> status for every one of the user's library entries — used at
    scoring time (issue #158) to check a candidate's PREQUEL relation against an
    already-owned earlier season's status."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT anime_id, status FROM library_entries WHERE user_id = %s",
            (USER_ID,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_recommendation_candidates(
    top_show_ids: list[int],
    library_ids: set[int],
) -> tuple[set[int], dict[int, int]]:
    """
    For each of the user's top completed shows, pull AniList community
    recommendations and collect anime IDs not already in the library.

    Also returns `vote_counts` (issue #226) — anime_id -> AniList's own per-edge
    community vote count (`Recommendation.rating`, net "yeah" minus "nah" votes),
    straight from data already in this payload, no extra API call. A candidate can
    appear as a recommendation off more than one of the user's top shows with a
    different rating each time; we keep the max, since that's the strongest real
    evidence available for that pairing and this is a display-only trust signal
    (no scoring impact — see issue #226's out-of-scope note). Non-positive/absent
    ratings are dropped rather than stored as 0/negative — "N AniList users agree"
    doesn't read sensibly for a net-negative or unrated pairing.
    """
    candidates: set[int] = set()
    vote_counts: dict[int, int] = {}
    for i, media_id in enumerate(top_show_ids, 1):
        print(f"  [{i}/{len(top_show_ids)}] Fetching recs for media id {media_id}...", flush=True)
        for page in range(1, RECS_PER_SHOW_PAGES + 1):
            data = gql(RECOMMENDATIONS_QUERY, {"mediaId": media_id, "page": page})
            recs = data["Media"]["recommendations"]
            for node in recs["nodes"]:
                rec = node.get("mediaRecommendation")
                if rec and rec["type"] == "ANIME" and rec["id"] not in library_ids:
                    candidates.add(rec["id"])
                    rating = node.get("rating")
                    if rating and rating > 0:
                        vote_counts[rec["id"]] = max(vote_counts.get(rec["id"], 0), rating)
            if not recs["pageInfo"]["hasNextPage"]:
                break
            time.sleep(INTER_REQUEST_SLEEP)
        time.sleep(INTER_REQUEST_SLEEP)
    return candidates, vote_counts


def current_season_year(now: datetime | None = None) -> tuple[str, int]:
    """Map the real calendar date to AniList's own season convention (issue #13):
    WINTER = Jan-Mar, SPRING = Apr-Jun, SUMMER = Jul-Sep, FALL = Oct-Dec. Computed
    in UTC so it stays correct regardless of the host's local timezone, and so it
    keeps being correct as time passes rather than needing a hardcoded season/year."""
    now = now or datetime.now(timezone.utc)
    month = now.month
    if month <= 3:
        season = "WINTER"
    elif month <= 6:
        season = "SPRING"
    elif month <= 9:
        season = "SUMMER"
    else:
        season = "FALL"
    return season, now.year


def fetch_seasonal_candidates(library_ids: set[int]) -> set[int]:
    """
    Issue #13 — seasonal discovery digest. Candidate sourcing by current
    season/year rather than similarity-to-library: pulls AniList's release list
    for the current season, batched via Page pagination (not a per-show loop) to
    stay well under the 90 req/min rate limit. Anime already in the user's
    library are excluded here so downstream scoring never has to special-case them.
    """
    season, year = current_season_year()
    print(f"  Season: {season} {year}")
    candidates: set[int] = set()
    page = 1
    while page <= SEASONAL_MAX_PAGES:
        data = gql(SEASONAL_IDS_QUERY, {"season": season, "seasonYear": year, "page": page})
        page_data = data["Page"]
        for media in page_data["media"]:
            if media["id"] not in library_ids:
                candidates.add(media["id"])
        if not page_data["pageInfo"]["hasNextPage"]:
            break
        page += 1
        time.sleep(INTER_REQUEST_SLEEP)
    return candidates


def _select_ids_to_fetch(
    conn, candidate_ids: set[int], staleness_days: int = CANDIDATE_STALENESS_DAYS
) -> set[int]:
    """Which candidate ids need a fresh AniList fetch this run (issue #254):
    anything with no `anime` row at all (never seen before), plus anything whose
    row exists but is older than `staleness_days` (a candidate discovered once and
    never touched again, e.g. Kingdom S2/S4/S5 pre-dating #158's `relations`
    capture). Deliberately excludes anything both known *and* still fresh — that's
    the caching behavior this replaces, kept intact for candidates that haven't had
    a chance to go stale yet."""
    if not candidate_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM anime WHERE id = ANY(%s) "
            "AND last_synced_at > now() - (%s * INTERVAL '1 day')",
            (list(candidate_ids), staleness_days),
        )
        fresh_ids = {row[0] for row in cur.fetchall()}
    return candidate_ids - fresh_ids


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


def _parse_relations(media: dict) -> list[dict]:
    """The exact `anime.relations` shape (issue #158): AniList's `relations.edges`
    flattened to just what the app/recommender need. Shared by _upsert_anime (the
    write path) and _make_prequel_relation_resolver below (issue #266's on-demand
    ancestor lookups) so both stay in sync with a single parsing definition."""
    return [
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
    # Mirrors sync_anilist.py's upsert_anime() relations handling (issue #158) — needed
    # here so brand-new recommender candidates (not yet in any user's library, so
    # sync_anilist.py has never touched them) still get relation data to check for an
    # unwatched PREQUEL at scoring time.
    relations = _parse_relations(media)
    cur.execute("""
        INSERT INTO anime (
            id, id_mal, title_romaji, title_english, title_native,
            format, status, episodes, season, season_year,
            genres, tags, studios, average_score,
            cover_image_url, banner_image_url, description,
            external_links, streaming_episodes, relations, last_synced_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
        ON CONFLICT (id) DO UPDATE SET
            title_english      = EXCLUDED.title_english,
            genres             = EXCLUDED.genres,
            tags               = EXCLUDED.tags,
            studios            = EXCLUDED.studios,
            average_score      = EXCLUDED.average_score,
            cover_image_url    = EXCLUDED.cover_image_url,
            banner_image_url   = EXCLUDED.banner_image_url,
            relations          = EXCLUDED.relations,
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
        json.dumps(ext_links), json.dumps(streaming), json.dumps(relations),
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

def score_candidate(
    media: dict,
    profile: dict,
    cross_user: dict | None = None,
    anilist_vote_count: int | None = None,
) -> tuple[float, dict]:
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
        # Issue #226 — AniList's own per-edge recommendation vote count, display-only
        # trust signal. Only ever set for candidates that actually appeared in
        # AniList's `recommendations` edge response (see fetch_recommendation_candidates);
        # never fabricated for PLANNING-list or cross-user-sourced candidates, which
        # have no equivalent AniList vote count.
        "anilist_vote_count": anilist_vote_count,
    }
    return raw, reason


# Statuses on an already-owned earlier season that mean "don't surface the sequel
# yet" — the user hasn't finished that earlier season, so recommending a later one
# is premature. COMPLETED is deliberately absent: finishing S1 with S2 not yet in
# the library is exactly the "watch next" case this recommender should still
# surface (issue #158).
PREQUEL_HIDE_STATUSES = {"WATCHING", "PLANNING", "PAUSED", "DROPPED"}

# Issue #266 — #158's suppression only ever checked a candidate's *immediate*
# PREQUEL edge, which silently did nothing whenever an intermediate season was
# never manually added to the library (the real Kingdom S4 case: S4 -> S3 [never
# added] -> S2 [never added] -> S1 [Paused] — single-hop found nothing at S3 and
# gave up, even though the real answer was two hops further back). This bounds
# how many PREQUEL hops _has_unwatched_prequel will walk backward looking for an
# ancestor's real library status. Kingdom — this repo's concrete motivating case
# — currently has 5 seasons; 10 gives any real franchise generous headroom to grow
# past that while still hard-capping the worst-case number of on-demand AniList
# lookups one candidate's suppression check can trigger (see
# _make_prequel_relation_resolver below). This is a per-candidate walk, not a
# catalog-wide loop, but still worth bounding explicitly rather than trusting
# AniList's relation graph to always be a short, finite chain — the visited-set
# in the walk below is the other half of that same defensiveness, for an actual
# graph cycle (which AniList's data model doesn't forbid).
MAX_PREQUEL_CHAIN_HOPS = 10


def _has_unwatched_prequel(
    media: dict,
    library_statuses: dict[int, str],
    resolve_relations=None,
    max_hops: int = MAX_PREQUEL_CHAIN_HOPS,
) -> bool:
    """True if `media`'s PREQUEL chain — walked backward hop by hop, not just the
    immediate prequel (issue #266, extending #158's single-hop check) — reaches an
    ancestor already in the user's library with a status in PREQUEL_HIDE_STATUSES.

    Walk semantics (see #266's acceptance criteria):
      - Suppress the moment ANY ancestor, at ANY distance, is found in the library
        with a hide-status.
      - A branch that reaches an ancestor found COMPLETED stops there without
        suppressing — the legitimate "watch next" case, unaffected by how far back
        it is (issue #158's original exception, unchanged).
      - An ancestor not in the library at all isn't evidence either way — keep
        walking through it, using `resolve_relations(anime_id)` to learn its own
        PREQUEL edges on demand. That's needed for ancestors that are neither in
        the library nor a scored candidate this run (e.g. Kingdom S3 above — no
        `anime` row for it at all). `resolve_relations` is optional and defaults
        to "don't look any further" so every pre-#266 call site/test keeps its
        original single-hop behavior unchanged.
      - Bounded by `max_hops` total hops popped off the walk queue, and a
        visited-set guards against a cyclical relation graph.
    """
    visited = {media.get("id")}
    queue: list[tuple[dict, int]] = [(media, 0)]
    while queue:
        current, hops = queue.pop(0)
        if hops >= max_hops:
            continue
        for rel in (current.get("relations") or []):
            if rel.get("relation_type") != "PREQUEL":
                continue
            prequel_id = rel.get("id")
            if prequel_id is None or prequel_id in visited:
                continue
            visited.add(prequel_id)
            status = library_statuses.get(prequel_id)
            if status in PREQUEL_HIDE_STATUSES:
                return True
            if status == "COMPLETED":
                continue  # resolved branch — stop here, don't walk past it
            if resolve_relations is None:
                continue
            ancestor_relations = resolve_relations(prequel_id)
            if ancestor_relations:
                queue.append(({"id": prequel_id, "relations": ancestor_relations}, hops + 1))
    return False


def _make_prequel_relation_resolver(conn):
    """Builds the `resolve_relations(anime_id)` callback _has_unwatched_prequel's
    chain walk uses to learn an ancestor's own PREQUEL edges (issue #266),
    memoized for the lifetime of one score_and_store() call — a shared ancestor
    (e.g. two candidates from the same franchise) is never looked up twice in the
    same run, and neither is the same ancestor across multiple candidates' walks.

    Checks the local `anime` row first. Falls back to a live AniList fetch —
    reusing the exact MEDIA_DETAILS_QUERY/gql()/_upsert_anime path
    fetch_and_store_candidates already uses, not a new query shape — only when
    that row is missing entirely or has never captured `relations` at all (an
    empty list is indistinguishable from "genuinely has no relations," so this
    doesn't force a refetch on every leaf, just on ones that could plausibly
    still be hiding real chain data).

    Deliberately does NOT consult #254's CANDIDATE_STALENESS_DAYS window here —
    per #266's "open questions": for a suppression check specifically, correctness
    (not re-showing a candidate whose earlier season is genuinely unwatched)
    matters more than the request-budget savings that window was tuned for, and
    the walk is already hard-bounded by MAX_PREQUEL_CHAIN_HOPS plus this cache, so
    the extra calls this can trigger stay small. #254's own staleness mechanism
    for top-level candidate refreshes (_select_ids_to_fetch) is untouched.
    """
    cache: dict[int, list] = {}

    def get_relations(anime_id: int) -> list:
        if anime_id in cache:
            return cache[anime_id]

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT relations FROM anime WHERE id = %s", (anime_id,))
            row = cur.fetchone()
        if row and row.get("relations"):
            cache[anime_id] = row["relations"]
            return row["relations"]

        try:
            data = gql(MEDIA_DETAILS_QUERY, {"ids": [anime_id], "page": 1})
            time.sleep(INTER_REQUEST_SLEEP)
        except Exception as e:
            print(
                f"  Warning: prequel-chain lookup failed for anime {anime_id}: {e}",
                file=sys.stderr,
            )
            cache[anime_id] = []
            return []

        media_list = (data.get("Page") or {}).get("media") or []
        if not media_list:
            cache[anime_id] = []
            return []

        media = media_list[0]
        with conn.cursor() as write_cur:
            _upsert_anime(write_cur, media)
        conn.commit()

        relations = _parse_relations(media)
        cache[anime_id] = relations
        return relations

    return get_relations


def score_and_store(
    conn,
    all_candidate_ids: set[int],
    profile: dict,
    sources: dict[int, str] | None = None,
    vote_counts: dict[int, int] | None = None,
) -> int:
    """`sources` maps anime_id -> 'similarity' | 'seasonal' (issue #13); any
    candidate not present in the map defaults to 'similarity', which keeps this
    backward-compatible with the original single-source call shape.

    `vote_counts` (issue #226) maps anime_id -> AniList's real per-edge
    recommendation vote count, for candidates that came from
    fetch_recommendation_candidates(); anything not present just gets None in the
    stored reason, deliberately — see score_candidate()'s docstring note."""
    sources = sources or {}
    vote_counts = vote_counts or {}
    cross_user_signal = fetch_cross_user_signal(conn, all_candidate_ids, USER_ID)
    library_statuses = get_library_statuses(conn)
    # issue #266 — one resolver per run, shared across every candidate's chain
    # walk below, so ancestors common to more than one candidate (or revisited
    # deeper in the same candidate's own chain) are only ever looked up once.
    resolve_relations = _make_prequel_relation_resolver(conn)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, genres, tags, studios, relations
            FROM anime WHERE id = ANY(%s)
        """, (list(all_candidate_ids),))
        media_rows = {row["id"]: dict(row) for row in cur.fetchall()}

    scored: list[tuple[int, float, dict]] = []
    for anime_id, media in media_rows.items():
        # issue #158 / #266 — skip candidates whose PREQUEL chain, walked back as
        # far as needed (not just the immediate prequel), reaches an earlier
        # season the user already owns but hasn't finished (WATCHING/PLANNING/
        # PAUSED/DROPPED). A COMPLETED earlier season anywhere in that chain is
        # the legitimate "watch next" case and must still be scored normally.
        if _has_unwatched_prequel(media, library_statuses, resolve_relations):
            continue
        raw, reason = score_candidate(
            media, profile, cross_user_signal.get(anime_id), vote_counts.get(anime_id)
        )
        scored.append((anime_id, raw, reason))

    # Normalise so the best candidate always hits 100
    if scored:
        max_raw = max(s[1] for s in scored) or 1.0
        scored = [(aid, (raw / max_raw) * 100.0, reason) for aid, raw, reason in scored]

    with conn.cursor() as cur:
        for anime_id, score, reason in scored:
            cur.execute("""
                INSERT INTO recommendation_scores (
                    user_id, anime_id, score, reason, source, dismissed, computed_at, first_shown_at
                )
                VALUES (%s, %s, %s, %s, %s, false, now(), now())
                ON CONFLICT (user_id, anime_id) DO UPDATE SET
                    score       = EXCLUDED.score,
                    reason      = EXCLUDED.reason,
                    source      = EXCLUDED.source,
                    computed_at = now()
                    -- dismissed, snoozed_until, and first_shown_at are intentionally
                    -- excluded from this SET clause: dismissed/snoozed_until are user
                    -- decisions that must survive re-runs (issue #75 extends the
                    -- original dismissed-preservation guarantee to cover the new
                    -- time-boxed "not now" snooze too). first_shown_at (issue #185)
                    -- must survive re-runs for a different reason — it's the anchor
                    -- the recommend->outcome hit-rate window is measured from, and a
                    -- rescore isn't a fresh recommendation, so it must not reset it.
            """, (USER_ID, anime_id, score, json.dumps(reason), sources.get(anime_id, "similarity")))
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
        print("Step 1/5 — Building taste profile from library...")
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

        print(f"\nStep 2/5 — Fetching AniList recommendations for top {len(top_ids)} shows...")
        rec_candidate_ids, rec_vote_counts = fetch_recommendation_candidates(top_ids, library_ids)
        print(f"  {len(rec_candidate_ids)} unique external candidates discovered")

        print("\nStep 3/5 — Fetching seasonal discovery digest candidates (issue #13)...")
        seasonal_candidate_ids = fetch_seasonal_candidates(library_ids)
        print(f"  {len(seasonal_candidate_ids)} unique seasonal candidates discovered")

        similarity_ids = rec_candidate_ids | planning_ids
        all_candidate_ids = similarity_ids | seasonal_candidate_ids
        print(f"  {len(all_candidate_ids)} total candidates to score")

        # Label each candidate by discovery path (issue #13's `source` column).
        # 'seasonal' wins on overlap — it's the more specific/informative label
        # ("new this season AND matches your taste") for the recommendations page's
        # "New this season" filter.
        sources: dict[int, str] = {aid: "similarity" for aid in similarity_ids}
        sources.update({aid: "seasonal" for aid in seasonal_candidate_ids})

        # Fetch/refresh details for candidates not yet in our anime table, or whose
        # row has gone stale (issue #254 — see CANDIDATE_STALENESS_DAYS above).
        to_fetch_ids = _select_ids_to_fetch(conn, all_candidate_ids)
        if to_fetch_ids:
            print(
                f"\nStep 4/5 — Fetching/refreshing details for {len(to_fetch_ids)} candidates "
                f"(new, or last synced more than {CANDIDATE_STALENESS_DAYS}d ago)..."
            )
            fetch_and_store_candidates(conn, list(to_fetch_ids))
        else:
            print("\nStep 4/5 — All candidate details already fresh, skipping fetch.")

        print(f"\nStep 5/5 — Scoring and storing {len(all_candidate_ids)} candidates...")
        n = score_and_store(conn, all_candidate_ids, profile, sources, rec_vote_counts)
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
