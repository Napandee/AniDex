"""
Coverage for issue #196 — "Your year so far": a living, always-current-calendar-year
destination (/stats/wrapped), the second half of the yearly-recap decision alongside
issue #193's #wrapup-card year-comparison extension on /stats.

Same two-tier pattern as tests/test_yearly_wrapped.py (#193's own test file):
- Pure-function unit tests for _pace_stat (issue #164's pace stat, folded into #196)
  — no DB needed, always run.
- DB-backed tests for _compute_wrapped_page, the notification trigger
  (_notify_wrapped_checkin), and the actual HTTP routes (/stats/wrapped renders,
  /stats links to it) — real Postgres via DATABASE_URL, skipped if unreachable.

The critical requirement this file exists to prove: #196 must call #193's exact
three reusable functions (_biggest_binge_week, _status_distribution_snapshot,
_score_distribution_shift) rather than a second, subtly different implementation.
test_compute_wrapped_page_calls_193s_exact_function_objects does this directly by
monkeypatching each of the three functions with a sentinel and asserting
_compute_wrapped_page's output reflects the sentinel — proof it's calling those
exact module-level functions, not inlined/duplicated logic.
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

USER_ID = 1


def _d(s):
    return datetime.date.fromisoformat(s)


# ── Pure-function unit coverage: _pace_stat (no DB) ─────────────────────────────

import app.main as m  # noqa: E402  (after sys.path insert, matches test_yearly_wrapped.py style)


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


def test_pace_stat_no_prior_year_data():
    this_year = [_row(_d("2026-03-01"), progress=12)]
    pace = m._pace_stat(this_year, [], cutoff_month_day=(4, 10))
    assert pace["status"] == "no_prior_data"
    assert pace["diff_pct"] is None
    assert pace["prior_year_episodes"] == 0


def test_pace_stat_ahead_of_last_year():
    # Cutoff (4, 10) — Apr 10. This year: 50 episodes by then. Last year: only 20
    # episodes by the *same* calendar-date cutoff, even though last year's row
    # set (if unfiltered) would total much more by year end — proving the
    # comparison is cutoff-scoped, not "this year vs. last year's full total"
    # (which the issue explicitly calls out as the wrong comparison).
    this_year = [_row(_d("2026-04-01"), progress=50)]
    prior_year = [
        _row(_d("2025-04-01"), progress=20),
        _row(_d("2025-11-01"), progress=200),  # past the cutoff — must be excluded
    ]
    pace = m._pace_stat(this_year, prior_year, cutoff_month_day=(4, 10))
    assert pace["status"] == "ahead"
    assert pace["prior_year_episodes"] == 20
    assert pace["this_year_episodes"] == 50
    assert pace["diff_pct"] == 150  # (50-20)/20 * 100


def test_pace_stat_behind_last_year():
    this_year = [_row(_d("2026-04-01"), progress=10)]
    prior_year = [_row(_d("2025-04-01"), progress=50)]
    pace = m._pace_stat(this_year, prior_year, cutoff_month_day=(4, 10))
    assert pace["status"] == "behind"
    assert pace["diff_pct"] == -80  # (10-50)/50 * 100


def test_pace_stat_on_pace_within_five_percent():
    this_year = [_row(_d("2026-04-01"), progress=102)]
    prior_year = [_row(_d("2025-04-01"), progress=100)]
    pace = m._pace_stat(this_year, prior_year, cutoff_month_day=(4, 10))
    assert pace["status"] == "on_pace"
    assert pace["diff_pct"] == 2


def test_pace_stat_excludes_rows_past_the_cutoff_in_both_years():
    # Same cutoff must be applied to both years' rows, not just the prior one —
    # a defensive symmetry check.
    this_year = [
        _row(_d("2026-01-10"), progress=5),
        _row(_d("2026-12-25"), progress=500),  # past the cutoff — excluded
    ]
    prior_year = [_row(_d("2025-01-10"), progress=5)]
    pace = m._pace_stat(this_year, prior_year, cutoff_month_day=(1, 20))
    assert pace["this_year_episodes"] == 5
    assert pace["status"] == "on_pace"


def test_pace_stat_leap_year_boundary_uses_calendar_date_not_ordinal():
    # Regression: 2028 is a leap year (Feb has 29 days) but 2027 isn't, so the
    # SAME calendar date falls on a different ordinal day-of-year in each —
    # 2028-03-05 is ordinal day 65, but so is 2027-03-06 (one calendar day
    # later). Comparing by ordinal day-of-year (the original implementation)
    # would have let 2027-03-06 leak into a 2028-03-05 cutoff; comparing by
    # (month, day) instead correctly excludes it.
    this_year = [_row(_d("2028-03-05"), progress=10)]
    prior_year = [
        _row(_d("2027-03-05"), progress=7, anime_id=1),    # same calendar date — included
        _row(_d("2027-03-06"), progress=100, anime_id=2),  # one day later — excluded
    ]
    pace = m._pace_stat(this_year, prior_year, cutoff_month_day=(3, 5))
    assert pace["prior_year_episodes"] == 7
    assert pace["this_year_episodes"] == 10


# ── DB-backed coverage ───────────────────────────────────────────────────────────


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
def db_module(pg_conn, monkeypatch):
    """NOT autouse, and cleanup lives here rather than in a separate
    autouse-on-pg_conn fixture — an autouse fixture that itself depends on
    pg_conn forces pg_conn to resolve (and skip, absent a reachable Postgres)
    for every test in this module, including the pure _pace_stat unit tests
    above that need no DB at all. Every DB-backed test below already requests
    db_module explicitly, so folding cleanup in here keeps the pure tests
    running with zero Postgres involvement, same as tests/test_yearly_wrapped.py."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as main_mod

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM users")

    return main_mod


@pytest.fixture()
def _seeded_user(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
            "VALUES (%s, 'local', 'test', 'test@example.com')",
            (USER_ID,),
        )


def _insert_anime(pg_conn, anime_id, genres=None, duration=24, title=None):
    import json as _json

    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, genres, duration) VALUES (%s, %s, %s::jsonb, %s)",
            (anime_id, title or f"Anime {anime_id}", _json.dumps(genres or []), duration),
        )


def _insert_entry(pg_conn, anime_id, finish_date, progress, score=None, user_id=USER_ID):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, score, finish_date) "
            "VALUES (%s, %s, 'COMPLETED', %s, %s, %s)",
            (user_id, anime_id, progress, score, finish_date),
        )


# ── The critical "reuse, not reimplement" requirement ───────────────────────────


def test_compute_wrapped_page_calls_193s_exact_function_objects(pg_conn, db_module, _seeded_user, monkeypatch):
    """Proof that _compute_wrapped_page calls #193's actual module-level functions
    (not a second, duplicated implementation): monkeypatching each of the three
    functions with a distinguishable sentinel changes the wrapped-page output —
    which is only possible if these exact objects are what gets invoked."""
    _insert_anime(pg_conn, 1, genres=["Action"])
    _insert_entry(pg_conn, 1, _d("2026-03-01"), progress=12, score=5)

    sentinel_binge = {"start": "SENTINEL", "end": "SENTINEL", "episodes": -999}
    sentinel_status = {"planning_to_completed": -999}
    sentinel_shift = {"this_year": [], "prior_year": [], "has_prior_year_data": "SENTINEL"}

    calls = {"binge": 0, "status": 0, "shift": 0}

    def fake_binge(rows):
        calls["binge"] += 1
        return sentinel_binge

    def fake_status(rows):
        calls["status"] += 1
        return sentinel_status

    def fake_shift(this_year_rows, prior_year_rows):
        calls["shift"] += 1
        return sentinel_shift

    monkeypatch.setattr(db_module, "_biggest_binge_week", fake_binge)
    monkeypatch.setattr(db_module, "_status_distribution_snapshot", fake_status)
    monkeypatch.setattr(db_module, "_score_distribution_shift", fake_shift)

    wrapped = db_module._compute_wrapped_page(USER_ID, today=_d("2026-06-15"))

    assert wrapped["binge_week"] == sentinel_binge
    assert wrapped["status_snapshot"] == sentinel_status
    assert wrapped["score_shift"] == sentinel_shift
    assert calls == {"binge": 1, "status": 1, "shift": 1}


def test_compute_wrapped_page_end_to_end_values(pg_conn, db_module, _seeded_user):
    _insert_anime(pg_conn, 1, genres=["Action", "Drama"], duration=24)
    _insert_anime(pg_conn, 2, genres=["Action"], duration=24)
    _insert_entry(pg_conn, 1, _d("2026-01-10"), progress=12, score=5)
    _insert_entry(pg_conn, 2, _d("2026-01-11"), progress=13, score=3)
    # Prior-year completion, for the score-distribution-shift comparison.
    _insert_anime(pg_conn, 3, genres=["Comedy"], duration=24)
    _insert_entry(pg_conn, 3, _d("2025-05-01"), progress=10, score=4)

    wrapped = db_module._compute_wrapped_page(USER_ID, today=_d("2026-06-15"))

    assert wrapped["year"] == 2026
    assert wrapped["prior_year"] == 2025
    assert wrapped["has_data"] is True
    assert wrapped["total_episodes"] == 25
    assert wrapped["total_minutes"] == 25 * 24
    assert wrapped["top_genre"] == "Action"  # appears on both anime 1 and 2
    assert wrapped["highest_rated"] == {"title": "Anime 1", "score": 5}
    assert wrapped["binge_week"]["episodes"] == 25  # both completions in same week
    assert wrapped["status_snapshot"] == {"planning_to_completed": 2}
    assert wrapped["score_shift"]["has_prior_year_data"] is True
    assert wrapped["pace"]["status"] in {"ahead", "behind", "on_pace"}


def test_compute_wrapped_page_no_completions_this_year_has_no_data(pg_conn, db_module, _seeded_user):
    wrapped = db_module._compute_wrapped_page(USER_ID, today=_d("2026-06-15"))
    assert wrapped["has_data"] is False
    assert wrapped["top_genre"] is None
    assert wrapped["highest_rated"] is None
    assert wrapped["binge_week"] is None
    assert wrapped["status_snapshot"] == {"planning_to_completed": 0}


def test_compute_wrapped_page_has_data_false_when_progress_is_zero(pg_conn, db_module, _seeded_user):
    """A COMPLETED row with a finish_date but zero progress (data-quality edge
    case — e.g. synced before progress was backfilled) must still read as "no
    data" for the page's empty-state gate, not render an all-zero headline row."""
    _insert_anime(pg_conn, 1, genres=["Action"])
    _insert_entry(pg_conn, 1, _d("2026-01-10"), progress=0, score=None)

    wrapped = db_module._compute_wrapped_page(USER_ID, today=_d("2026-06-15"))

    assert wrapped["total_episodes"] == 0
    assert wrapped["has_data"] is False


def test_compute_wrapped_page_top_genre_tiebreak_is_deterministic(pg_conn, db_module, _seeded_user):
    """_yearly_completion_rows has no ORDER BY, so a tied genre count must not
    depend on unspecified row order — deterministically resolves to the
    alphabetically-first genre name."""
    _insert_anime(pg_conn, 1, genres=["Zeta"])
    _insert_anime(pg_conn, 2, genres=["Alpha"])
    _insert_entry(pg_conn, 1, _d("2026-01-10"), progress=1)
    _insert_entry(pg_conn, 2, _d("2026-01-11"), progress=1)

    wrapped = db_module._compute_wrapped_page(USER_ID, today=_d("2026-06-15"))

    assert wrapped["top_genre"] == "Alpha"


def test_compute_wrapped_page_highest_rated_tiebreak_is_deterministic(pg_conn, db_module, _seeded_user):
    """Same determinism guarantee for a tied top score — resolves to the
    alphabetically-first title rather than whichever row Postgres returns first."""
    _insert_anime(pg_conn, 1, genres=["Action"], title="Zeta Show")
    _insert_anime(pg_conn, 2, genres=["Action"], title="Alpha Show")
    _insert_entry(pg_conn, 1, _d("2026-01-10"), progress=1, score=5)
    _insert_entry(pg_conn, 2, _d("2026-01-11"), progress=1, score=5)

    wrapped = db_module._compute_wrapped_page(USER_ID, today=_d("2026-06-15"))

    assert wrapped["highest_rated"] == {"title": "Alpha Show", "score": 5}


def test_notify_wrapped_checkin_raises_on_unknown_occasion(pg_conn, db_module):
    with pytest.raises(ValueError):
        db_module._notify_wrapped_checkin("mid_year")  # not "midyear" — must not
                                                         # silently fall through to
                                                         # the year_end branch


# ── Notification trigger (issue #51 dispatcher integration) ────────────────────


def test_notify_wrapped_checkin_fires_for_user_with_data(pg_conn, db_module, _seeded_user, monkeypatch):
    _insert_anime(pg_conn, 1, genres=["Action"])
    _insert_entry(pg_conn, 1, _d("2026-01-10"), progress=12, score=5)

    sent = []
    monkeypatch.setattr(db_module, "notify", lambda user_id, title, body: sent.append((user_id, title, body)))

    db_module._notify_wrapped_checkin("midyear")

    assert len(sent) == 1
    user_id, title, body = sent[0]
    assert user_id == USER_ID
    assert "/stats/wrapped" in body


def test_notify_wrapped_checkin_skips_user_with_no_completions_this_year(pg_conn, db_module, _seeded_user, monkeypatch):
    sent = []
    monkeypatch.setattr(db_module, "notify", lambda user_id, title, body: sent.append((user_id, title, body)))

    db_module._notify_wrapped_checkin("midyear")

    assert sent == []


def test_notify_wrapped_checkin_midyear_and_year_end_bodies_differ(pg_conn, db_module, _seeded_user, monkeypatch):
    _insert_anime(pg_conn, 1, genres=["Action"])
    _insert_entry(pg_conn, 1, _d("2026-01-10"), progress=12, score=5)

    sent = []
    monkeypatch.setattr(db_module, "notify", lambda user_id, title, body: sent.append((user_id, title, body)))

    db_module._notify_wrapped_checkin("midyear")
    db_module._notify_wrapped_checkin("year_end")

    assert len(sent) == 2
    assert sent[0][2] != sent[1][2]
    assert "/stats/wrapped" in sent[0][2]
    assert "/stats/wrapped" in sent[1][2]


def test_notify_wrapped_checkin_one_user_failure_does_not_block_others(pg_conn, db_module, monkeypatch):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) VALUES (1, 'local', 'a', 'a@example.com')"
        )
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) VALUES (2, 'local', 'b', 'b@example.com')"
        )
    _insert_anime(pg_conn, 1, genres=["Action"])
    _insert_entry(pg_conn, 1, _d("2026-01-10"), progress=12, score=5, user_id=1)
    _insert_entry(pg_conn, 1, _d("2026-01-10"), progress=12, score=5, user_id=2)

    real_compute = db_module._compute_wrapped_page

    def flaky_compute(user_id, today=None):
        if user_id == 1:
            raise RuntimeError("boom")
        return real_compute(user_id, today)

    monkeypatch.setattr(db_module, "_compute_wrapped_page", flaky_compute)
    sent = []
    monkeypatch.setattr(db_module, "notify", lambda user_id, title, body: sent.append(user_id))

    db_module._notify_wrapped_checkin("midyear")

    # user 1's computation raised, user 2 must still have been notified.
    assert sent == [2]


# ── HTTP routes ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(db_module):
    from starlette.testclient import TestClient

    with TestClient(db_module.app) as c:
        yield c


def _register_and_login(client, email="owner@example.com", password="correct horse battery staple"):
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def test_wrapped_page_requires_auth(pg_conn, db_module, client):
    resp = client.get("/stats/wrapped", follow_redirects=False)
    assert resp.status_code in (302, 303)


def test_wrapped_page_renders_with_real_multiyear_data(pg_conn, db_module, client):
    """Render the page through Jinja2/TestClient with real synthetic data spanning
    two calendar years — this app has had a real production incident from an
    assumed-safe template (fastapi/starlette TemplateResponse breakage, see
    CLAUDE.local.md), so this isn't skipped in favor of only unit-testing the data
    layer above."""
    _register_and_login(client, email="wrapped-render@example.com")
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = 'wrapped-render@example.com'")
        (uid,) = cur.fetchone()

    today = datetime.date.today()
    _insert_anime(pg_conn, 1, genres=["Action", "Drama"])
    _insert_anime(pg_conn, 2, genres=["Comedy"])
    _insert_entry(pg_conn, 1, today, progress=12, score=5, user_id=uid)
    _insert_entry(pg_conn, 2, today, progress=6, score=4, user_id=uid)
    # Prior-year data too, so the score-shift/pace branches with real prior-year
    # data actually render (not just the empty-state branch).
    prior_year_date = today.replace(year=today.year - 1) if not (today.month == 2 and today.day == 29) else today.replace(year=today.year - 1, day=28)
    _insert_anime(pg_conn, 3, genres=["Comedy"])
    _insert_entry(pg_conn, 3, prior_year_date, progress=10, score=3, user_id=uid)

    resp = client.get("/stats/wrapped")
    assert resp.status_code == 200
    assert "18" in resp.text  # total_episodes = 12 + 6
    assert "Action" in resp.text  # top genre

    # Real template content rendered (not an error page / empty shell).
    assert "★" in resp.text


def test_wrapped_page_renders_empty_state_with_no_completions(pg_conn, db_module, client):
    _register_and_login(client, email="wrapped-empty@example.com")
    resp = client.get("/stats/wrapped")
    assert resp.status_code == 200


def test_stats_page_links_to_wrapped_page(pg_conn, db_module, client):
    _register_and_login(client, email="stats-link@example.com")
    resp = client.get("/stats")
    assert resp.status_code == 200
    assert 'href="/stats/wrapped"' in resp.text
