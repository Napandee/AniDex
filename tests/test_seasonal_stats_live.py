"""
Live verification for issue #224 (anime-native format split + seasonal
follow-through rate) against a real Postgres and the real GET /api/stats route
— not just the unit-level FakeDB coverage in tests/test_seasonal_stats.py.

Verified against a real Postgres, same pattern as tests/test_streaming_coverage.py
and tests/test_collections.py: a real FastAPI TestClient driving the actual
GET /api/stats HTTP route against the real schema.sql shape, with realistic
anime/library_entries rows (mixed TV/movie formats, mixed season/season_year,
mixed start_date placement relative to the airing window).

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no
Postgres running.
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


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
    yield conn
    conn.close()


@pytest.fixture()
def app_module(pg_conn, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    return m


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM users")


@pytest.fixture()
def client(app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


def _register_and_login(client, pg_conn, email="owner@example.com", password="correct horse battery staple"):
    """Registers + logs in, then returns the real assigned user id -- `_clean_tables`
    below uses DELETE (not TRUNCATE ... RESTART IDENTITY), same as
    tests/test_streaming_coverage.py, so the `users.id` serial keeps advancing
    across tests in this module and must never be assumed to be 1."""
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        return cur.fetchone()[0]


def _insert_anime(pg_conn, anime_id, format, season=None, season_year=None,
                   duration=None, episodes=None, status="FINISHED", title=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, format, season, season_year, "
            "duration, episodes, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (anime_id, title or f"Anime {anime_id}", format, season, season_year,
             duration, episodes, status),
        )


def _insert_entry(pg_conn, anime_id, status, progress, user_id, start_date=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, start_date) "
            "VALUES (%s, %s, %s, %s, %s)",
            (user_id, anime_id, status, progress, start_date),
        )


def test_stats_page_renders_with_new_sections(pg_conn, app_module, client):
    """Sanity check the page itself still renders (no template error) with the
    two new format-split/seasonal-follow-through blocks present in the markup."""
    _register_and_login(client, pg_conn)
    resp = client.get("/stats")
    assert resp.status_code == 200, resp.text
    assert "seasonal-follow-through-section" in resp.text
    assert "format-split-headlines" in resp.text


def test_api_stats_format_split_separates_episodes_from_movies(pg_conn, app_module, client):
    user_id = _register_and_login(client, pg_conn)

    _insert_anime(pg_conn, 1, format="TV", duration=23, episodes=24, title="TV Show")
    _insert_entry(pg_conn, 1, status="COMPLETED", progress=24, user_id=user_id)

    _insert_anime(pg_conn, 2, format="MOVIE", duration=107, episodes=1, title="Movie A")
    _insert_entry(pg_conn, 2, status="COMPLETED", progress=1, user_id=user_id)

    _insert_anime(pg_conn, 3, format="MOVIE", duration=None, episodes=1, title="Movie B (no duration)")
    _insert_entry(pg_conn, 3, status="COMPLETED", progress=1, user_id=user_id)

    resp = client.get("/api/stats")
    assert resp.status_code == 200, resp.text
    fs = resp.json()["format_split"]

    assert fs["episode_count"] == 24
    assert fs["movie_count"] == 2
    # 107 (real duration) + 100 (MOVIE_DEFAULT_DURATION_MINUTES fallback)
    assert fs["movie_minutes"] == 207
    assert fs["movie_hours"] == round(207 / 60, 1)


def test_api_stats_seasonal_follow_through_end_to_end(pg_conn, app_module, client):
    """Seeds a realistic mix: some shows started right when they began airing
    (a mix of completed and dropped/stalled outcomes), one started via a much
    later binge (must be excluded per issue #224's explicit out-of-scope note),
    one still currently airing (excluded from the rate, tracked separately),
    and one movie (excluded — not a seasonal weekly-release concept). Verifies
    the real GET /api/stats route end to end, not just the FakeDB unit tests.
    """
    user_id = _register_and_login(client, pg_conn)

    # Five WINTER 2026 TV shows started right at the top of the season.
    _insert_anime(pg_conn, 10, format="TV", season="WINTER", season_year=2026, status="FINISHED", title="Kept up 1")
    _insert_entry(pg_conn, 10, status="COMPLETED", progress=12, user_id=user_id, start_date=date(2026, 1, 8))

    _insert_anime(pg_conn, 11, format="TV", season="WINTER", season_year=2026, status="FINISHED", title="Kept up 2")
    _insert_entry(pg_conn, 11, status="REPEATING", progress=12, user_id=user_id, start_date=date(2026, 1, 10))

    _insert_anime(pg_conn, 12, format="TV", season="WINTER", season_year=2026, status="FINISHED", title="Dropped")
    _insert_entry(pg_conn, 12, status="DROPPED", progress=4, user_id=user_id, start_date=date(2026, 1, 9))

    _insert_anime(pg_conn, 13, format="TV", season="WINTER", season_year=2026, status="FINISHED", title="Stalled")
    _insert_entry(pg_conn, 13, status="WATCHING", progress=5, user_id=user_id, start_date=date(2026, 1, 7))

    _insert_anime(pg_conn, 14, format="TV", season="WINTER", season_year=2026, status="FINISHED", title="Paused-stalled")
    _insert_entry(pg_conn, 14, status="PAUSED", progress=3, user_id=user_id, start_date=date(2026, 1, 12))

    # A binge long after the WINTER 2026 window closed -- must be excluded entirely.
    _insert_anime(pg_conn, 15, format="TV", season="WINTER", season_year=2026, status="FINISHED", title="Late binge")
    _insert_entry(pg_conn, 15, status="COMPLETED", progress=12, user_id=user_id, start_date=date(2026, 9, 1))

    # A show still currently airing that the user started on time -- excluded
    # from the rate (no verdict yet), tracked separately.
    _insert_anime(pg_conn, 16, format="TV", season="SUMMER", season_year=2026, status="RELEASING", title="Still airing")
    _insert_entry(pg_conn, 16, status="WATCHING", progress=3, user_id=user_id, start_date=date(2026, 7, 5))

    # A movie in the same season -- excluded, not a seasonal weekly concept.
    _insert_anime(pg_conn, 17, format="MOVIE", season="WINTER", season_year=2026, status="FINISHED", title="Movie")
    _insert_entry(pg_conn, 17, status="COMPLETED", progress=1, user_id=user_id, start_date=date(2026, 1, 9))

    resp = client.get("/api/stats")
    assert resp.status_code == 200, resp.text
    sft = resp.json()["seasonal_follow_through"]

    assert sft is not None, "expected 5 judged entries to clear SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES"
    assert sft["followed_through"] == 2
    assert sft["dropped_or_stalled"] == 3
    assert sft["judged"] == 5
    assert sft["excluded_still_airing"] == 1
    assert sft["rate"] == round(2 / 5 * 100)


def test_api_stats_seasonal_follow_through_none_below_sample_gate(pg_conn, app_module, client):
    user_id = _register_and_login(client, pg_conn)
    _insert_anime(pg_conn, 20, format="TV", season="WINTER", season_year=2026, status="FINISHED", title="Only one")
    _insert_entry(pg_conn, 20, status="COMPLETED", progress=12, user_id=user_id, start_date=date(2026, 1, 8))

    resp = client.get("/api/stats")
    assert resp.status_code == 200, resp.text
    assert resp.json()["seasonal_follow_through"] is None
