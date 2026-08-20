"""
Coverage for the three year-comparison aggregations issue #193 adds to #wrapup-card
(issue #78's "Year in anime" card on /stats): biggest single-week binge,
Planning->Completed status-distribution snapshot, and year-over-year
score-distribution shift.

Split into two tiers, same pattern as tests/test_admin_instance_health.py:

- Pure-function unit tests (_biggest_binge_week, _status_distribution_snapshot,
  _score_distribution, _score_distribution_shift) — no DB needed, always run.
  These are exactly the "reusable functions/helpers" issue #193 requires so a
  later held issue (#196, the always-current-year "living page") can call the
  same logic without duplicating the query/aggregation code.
- _yearly_completion_rows / _year_comparison_extras — real Postgres via
  DATABASE_URL, skipped entirely if unreachable (same throwaway-Postgres pattern
  .github/workflows/pr-validate.yml provisions), since these actually run SQL
  against the real schema.sql shape rather than a hand-duplicated subset of it.
"""

import datetime
import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


# ── Pure-function unit coverage (no DB) ───────────────────────────────────────

import app.main as m  # noqa: E402  (after sys.path insert, matches test_rewatch_notes.py style)


def _row(finish_date, progress=1, score=None, genres=None, title="Test", anime_id=1, duration=24):
    return {
        "anime_id": anime_id,
        "finish_date": finish_date,
        "progress": progress,
        "score": score,
        "title_english": title,
        "title_romaji": title,
        "genres": genres or [],
        "duration": duration,
    }


def _d(s):
    return datetime.date.fromisoformat(s)


def test_biggest_binge_week_empty_rows():
    assert m._biggest_binge_week([]) is None


def test_biggest_binge_week_single_day():
    rows = [_row(_d("2026-03-10"), progress=5)]
    result = m._biggest_binge_week(rows)
    assert result == {"start": "2026-03-10", "end": "2026-03-16", "episodes": 5}


def test_biggest_binge_week_picks_the_denser_window():
    # Two clusters: 2026-01-01..01-03 (3+4+2=9 episodes) vs. a lone 2026-06-01 (20
    # episodes on its own). The lone day should win even though the cluster has
    # more individual entries, since the window sum is what matters.
    rows = [
        _row(_d("2026-01-01"), progress=3),
        _row(_d("2026-01-02"), progress=4),
        _row(_d("2026-01-03"), progress=2),
        _row(_d("2026-06-01"), progress=20),
    ]
    result = m._biggest_binge_week(rows)
    assert result["episodes"] == 20
    assert result["start"] == "2026-06-01"


def test_biggest_binge_week_sums_within_seven_day_window():
    # 2026-02-01 and 2026-02-07 are both within the 7-day window starting 2026-02-01
    # (inclusive end = start + 6 days = 2026-02-07), so their episodes combine (12).
    # The window anchored at 2026-02-07 (end 2026-02-13) also reaches 2026-02-08,
    # combining 6 + 100 = 106 — the actual maximum, since the sliding window is
    # tested at every distinct finish_date, not just the earliest one.
    rows = [
        _row(_d("2026-02-01"), progress=6),
        _row(_d("2026-02-07"), progress=6),
        _row(_d("2026-02-08"), progress=100),
    ]
    result = m._biggest_binge_week(rows)
    assert result["episodes"] == 106
    assert result["start"] == "2026-02-07"
    assert result["end"] == "2026-02-13"


def test_biggest_binge_week_multiple_entries_same_day_combine():
    rows = [
        _row(_d("2026-04-05"), progress=12, anime_id=1),
        _row(_d("2026-04-05"), progress=13, anime_id=2),
    ]
    result = m._biggest_binge_week(rows)
    assert result["episodes"] == 25


def test_status_distribution_snapshot_counts_rows():
    assert m._status_distribution_snapshot([]) == {"planning_to_completed": 0}
    rows = [_row(_d("2026-01-01")), _row(_d("2026-06-01")), _row(_d("2026-12-31"))]
    assert m._status_distribution_snapshot(rows) == {"planning_to_completed": 3}


def test_score_distribution_ignores_null_and_zero_scores():
    rows = [
        _row(_d("2026-01-01"), score=4),
        _row(_d("2026-01-02"), score=4),
        _row(_d("2026-01-03"), score=None),
        _row(_d("2026-01-04"), score=0),
        _row(_d("2026-01-05"), score=5),
    ]
    assert m._score_distribution(rows) == [{"score": 4, "count": 2}, {"score": 5, "count": 1}]


def test_score_distribution_shift_with_prior_year_data():
    this_year = [_row(_d("2026-05-01"), score=5), _row(_d("2026-05-02"), score=5)]
    prior_year = [_row(_d("2025-05-01"), score=3)]
    shift = m._score_distribution_shift(this_year, prior_year)
    assert shift["has_prior_year_data"] is True
    assert shift["this_year"] == [{"score": 5, "count": 2}]
    assert shift["prior_year"] == [{"score": 3, "count": 1}]


def test_score_distribution_shift_gracefully_handles_no_prior_year_data():
    """Issue #193's explicit graceful-empty-state requirement: a user's first year
    on AniDex (or simply no scored completions the year before) must not error."""
    this_year = [_row(_d("2026-05-01"), score=5)]
    shift = m._score_distribution_shift(this_year, [])
    assert shift["has_prior_year_data"] is False
    assert shift["prior_year"] == []
    assert shift["this_year"] == [{"score": 5, "count": 1}]


# ── DB-backed coverage: _yearly_completion_rows / _year_comparison_extras ────


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
            "VALUES (1, 'local', 'test', 'test@example.com')"
        )
        cur.execute("INSERT INTO anime (id, title_romaji, genres, duration) "
                    "VALUES (1, 'Show A', '[\"Action\"]', 24)")
        cur.execute("INSERT INTO anime (id, title_romaji, genres, duration) "
                    "VALUES (2, 'Show B', '[\"Comedy\"]', 24)")
        cur.execute("INSERT INTO anime (id, title_romaji, genres, duration) "
                    "VALUES (3, 'Show C', '[\"Action\"]', 24)")
    yield conn
    conn.close()


@pytest.fixture()
def db_module(pg_conn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as main_mod

    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE library_entries RESTART IDENTITY")
    return main_mod


def test_yearly_completion_rows_scopes_by_calendar_year(pg_conn, db_module):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, finish_date) "
            "VALUES (1, 1, 'COMPLETED', 12, '2025-12-31')"
        )
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, finish_date) "
            "VALUES (1, 2, 'COMPLETED', 24, '2026-01-01')"
        )
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress) "
            "VALUES (1, 3, 'WATCHING', 3)"  # no finish_date — must never appear
        )

    rows_2025 = db_module._yearly_completion_rows(1, 2025)
    rows_2026 = db_module._yearly_completion_rows(1, 2026)

    assert [r["anime_id"] for r in rows_2025] == [1]
    assert [r["anime_id"] for r in rows_2026] == [2]


def test_year_comparison_extras_end_to_end(pg_conn, db_module):
    with pg_conn.cursor() as cur:
        # 2026: two completions in the same week (binge), one scored 5.
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, score, finish_date) "
            "VALUES (1, 1, 'COMPLETED', 12, 5, '2026-03-01')"
        )
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, score, finish_date) "
            "VALUES (1, 2, 'COMPLETED', 13, 4, '2026-03-02')"
        )
        # 2025: one completion, different score, for the year-over-year comparison.
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, score, finish_date) "
            "VALUES (1, 3, 'COMPLETED', 10, 3, '2025-07-01')"
        )

    extras = db_module._year_comparison_extras(1, 2026)

    assert extras["year"] == 2026
    assert extras["prior_year"] == 2025
    assert extras["binge_week"]["episodes"] == 25  # 12 + 13, same 7-day window
    assert extras["status_snapshot"] == {"planning_to_completed": 2}
    assert extras["score_shift"]["has_prior_year_data"] is True
    assert extras["score_shift"]["this_year"] == [{"score": 4, "count": 1}, {"score": 5, "count": 1}]
    assert extras["score_shift"]["prior_year"] == [{"score": 3, "count": 1}]


def test_year_comparison_extras_first_year_has_no_prior_data(pg_conn, db_module):
    """Issue #193's explicit acceptance criterion: a user's first year on AniDex
    must degrade gracefully, not error, when there's nothing to compare against."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, score, finish_date) "
            "VALUES (1, 1, 'COMPLETED', 12, 5, '2026-03-01')"
        )

    extras = db_module._year_comparison_extras(1, 2026)

    assert extras["score_shift"]["has_prior_year_data"] is False
    assert extras["score_shift"]["prior_year"] == []
    assert extras["binge_week"]["episodes"] == 12
    assert extras["status_snapshot"] == {"planning_to_completed": 1}
