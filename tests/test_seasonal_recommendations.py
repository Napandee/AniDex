"""
Coverage for issue #13 — the seasonal discovery digest ("new this season, matches
your taste profile, not in your library yet"), additive to the existing
similarity-based recommender path.

Split into two layers, same pattern as tests/test_sync_crunchyroll.py (pure-function
unit tests, no Postgres needed) and tests/test_recommendation_snooze.py (real-Postgres
coverage for the `source` column / query behavior added by migration 012):

  1. current_season_year() — pure calendar-to-AniList-season mapping, no I/O.
  2. fetch_seasonal_candidates() — batched Page pagination + library exclusion,
     with AniList's gql() monkeypatched so no real network call is made.
  3. score_and_store() / _fetch_visible_recommendations() — real Postgres, verifying
     the `source` column is written/read correctly and that seasonal candidates
     aren't starved out of the recommendations view by similarity candidates (or
     vice versa).
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_recommender as rr

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

USER_ID = 1


# ---------------------------------------------------------------------------
# current_season_year() — pure function, no I/O
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "month,expected_season",
    [
        (1, "WINTER"), (2, "WINTER"), (3, "WINTER"),
        (4, "SPRING"), (5, "SPRING"), (6, "SPRING"),
        (7, "SUMMER"), (8, "SUMMER"), (9, "SUMMER"),
        (10, "FALL"), (11, "FALL"), (12, "FALL"),
    ],
)
def test_current_season_year_maps_every_month(month, expected_season):
    season, year = rr.current_season_year(datetime(2026, month, 15, tzinfo=timezone.utc))
    assert season == expected_season
    assert year == 2026


def test_current_season_year_defaults_to_now():
    # No `now` passed — must not raise, and must return a valid AniList season.
    season, year = rr.current_season_year()
    assert season in ("WINTER", "SPRING", "SUMMER", "FALL")
    assert year >= 2026


# ---------------------------------------------------------------------------
# fetch_seasonal_candidates() — gql() monkeypatched, no real network call
# ---------------------------------------------------------------------------

def test_fetch_seasonal_candidates_excludes_library_and_paginates(monkeypatch):
    calls = []

    def fake_gql(query, variables, retries=5):
        calls.append(variables)
        page = variables["page"]
        if page == 1:
            return {"Page": {"pageInfo": {"hasNextPage": True}, "media": [{"id": 1}, {"id": 2}]}}
        if page == 2:
            return {"Page": {"pageInfo": {"hasNextPage": False}, "media": [{"id": 3}]}}
        raise AssertionError("should not page past hasNextPage=False")

    monkeypatch.setattr(rr, "gql", fake_gql)
    monkeypatch.setattr(rr.time, "sleep", lambda *_: None)

    result = rr.fetch_seasonal_candidates(library_ids={2})

    assert result == {1, 3}  # id 2 excluded — already in library
    assert len(calls) == 2  # stopped once hasNextPage was False
    assert calls[0]["season"] in ("WINTER", "SPRING", "SUMMER", "FALL")


def test_fetch_seasonal_candidates_respects_max_pages(monkeypatch):
    calls = []

    def fake_gql(query, variables, retries=5):
        calls.append(variables["page"])
        # Always claims another page is available — the max-pages cap must still
        # stop the loop so a single run can't balloon past the rate-limit budget.
        return {"Page": {"pageInfo": {"hasNextPage": True}, "media": [{"id": variables["page"]}]}}

    monkeypatch.setattr(rr, "gql", fake_gql)
    monkeypatch.setattr(rr.time, "sleep", lambda *_: None)

    result = rr.fetch_seasonal_candidates(library_ids=set())

    assert len(calls) == rr.SEASONAL_MAX_PAGES
    assert result == set(range(1, rr.SEASONAL_MAX_PAGES + 1))


# ---------------------------------------------------------------------------
# Real-Postgres coverage for the `source` column (migration 012)
# ---------------------------------------------------------------------------

def _try_connect():
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=2)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception:
        return None


@pytest.fixture(scope="module")
def pg_conn():
    conn = _try_connect()
    if conn is None:
        pytest.skip(
            f"No reachable Postgres at {DATABASE_URL} — this suite needs a real "
            "throwaway instance (same one .github/workflows/pr-validate.yml provisions)."
        )
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(SCHEMA_SQL)
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
            "VALUES (%s, 'local', 'test', 'test@example.com')",
            (USER_ID,),
        )
        for anime_id, title, season, season_year in [
            (601, "Similarity Pick", None, None),
            (602, "Seasonal Pick", "FALL", 2026),
        ]:
            cur.execute(
                "INSERT INTO anime (id, title_romaji, season, season_year) VALUES (%s, %s, %s, %s)",
                (anime_id, title, season, season_year),
            )
    yield conn
    conn.close()


@pytest.fixture()
def clean_recommendation_scores(pg_conn):
    """Explicit (not autouse) — this module also has pure-function tests above
    that don't touch Postgres at all and must not be forced to skip just because
    no Postgres is reachable; only the DB-backed tests below request this."""
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM recommendation_scores")


@pytest.fixture()
def fetch_visible(pg_conn, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    return m._fetch_visible_recommendations


def test_score_and_store_writes_source_column(pg_conn, clean_recommendation_scores):
    profile = {"genres": {}, "tags": {}, "studios": {}}
    old_user_id = rr.USER_ID
    rr.USER_ID = USER_ID
    try:
        rr.score_and_store(
            pg_conn,
            {601, 602},
            profile,
            sources={601: "similarity", 602: "seasonal"},
        )
    finally:
        rr.USER_ID = old_user_id

    with pg_conn.cursor() as cur:
        cur.execute("SELECT anime_id, source FROM recommendation_scores ORDER BY anime_id")
        rows = cur.fetchall()

    assert rows == [(601, "similarity"), (602, "seasonal")]


def test_score_and_store_defaults_source_when_unmapped(pg_conn, clean_recommendation_scores):
    """Backward-compat: the original 3-positional-arg call shape (no `sources`
    kwarg) must still work and default every candidate to 'similarity'."""
    profile = {"genres": {}, "tags": {}, "studios": {}}
    old_user_id = rr.USER_ID
    rr.USER_ID = USER_ID
    try:
        rr.score_and_store(pg_conn, {601}, profile)
    finally:
        rr.USER_ID = old_user_id

    with pg_conn.cursor() as cur:
        cur.execute("SELECT source FROM recommendation_scores WHERE anime_id = 601")
        (source,) = cur.fetchone()

    assert source == "similarity"


def test_fetch_visible_recommendations_includes_both_sources(pg_conn, fetch_visible, clean_recommendation_scores):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, source) "
            "VALUES (%s, 601, 80.0, 'similarity'), (%s, 602, 40.0, 'seasonal')",
            (USER_ID, USER_ID),
        )

    rows = fetch_visible(USER_ID)
    by_id = {r["id"]: r for r in rows}

    assert set(by_id) == {601, 602}
    assert by_id[601]["source"] == "similarity"
    assert by_id[602]["source"] == "seasonal"
    assert by_id[602]["season"] == "FALL"
    assert by_id[602]["season_year"] == 2026


def test_fetch_visible_recommendations_seasonal_not_starved_by_similarity(pg_conn, fetch_visible, clean_recommendation_scores):
    """A low-scoring seasonal candidate must still surface even when similarity
    candidates dominate the top of the global score ranking — per-source LIMIT
    partitioning (issue #13), not one global LIMIT 100 shared across both."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, source) "
            "VALUES (%s, 601, 99.0, 'similarity'), (%s, 602, 1.0, 'seasonal')",
            (USER_ID, USER_ID),
        )

    rows = fetch_visible(USER_ID)
    ids = {r["id"] for r in rows}
    assert 602 in ids
