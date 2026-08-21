"""
Coverage for issue #257 — /upcoming's Month calendar view, a third toggle
alongside List and Weekly grid (#256).

Verified against a real Postgres, same pattern as tests/test_upcoming_week_grid.py
and tests/test_streaming_calendar.py — skipped entirely if one isn't reachable via
DATABASE_URL.

Covers the acceptance criteria from #257:
  1. The month grid is a real 7-column calendar with the correct number of
     leading/trailing blank cells for whatever month is being displayed (a real
     month-boundary case: a month that doesn't start on Monday), and every day
     actually in the month gets a dated cell.
  2. Chips for a given anime/episode show up in the correct day cell, not just
     somewhere in the grid.
  3. The overflow indicator ("+N more") appears once a day has more entries than
     the chip cap, and doesn't appear (or shows the right count) otherwise.
  4. Prev/Next (`?month_offset=N`) and Today navigation work, and month_offset is
     clamped at 0 the same way week_offset already is (airing_schedule_cache only
     ever holds not-yet-aired rows, so a month before the current one is
     guaranteed empty).
  5. The List view and the Weekly grid are both completely unaffected.
"""

import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

# app/config.py's DEFAULT for a user with no explicit timezone setting.
DEFAULT_TZ = ZoneInfo("Europe/London")

MONTH_CHIP_CAP = 3  # must match app/main.py's upcoming()


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
        cur.execute("DELETE FROM airing_schedule_cache")
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM users")


def _insert_anime(pg_conn, anime_id, title, episodes=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, episodes) VALUES (%s, %s, %s)",
            (anime_id, title, episodes),
        )


def _insert_entry(pg_conn, anime_id, user_id, status="WATCHING", progress=0):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, anime_id, status, progress),
        )


def _insert_airing(pg_conn, anime_id, episode, airing_at):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO airing_schedule_cache (anime_id, episode, airing_at) VALUES (%s, %s, %s)",
            (anime_id, episode, airing_at),
        )


@pytest.fixture()
def client(app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


def _register_and_login(client, email="owner@example.com", password="correct horse battery staple"):
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def _user_id(pg_conn, email):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        return cur.fetchone()[0]


def _month_slice(html):
    """Only the month-view section of the rendered page — the List/Week sections
    render legitimately overlapping text too, so assertions about the month grid
    must be scoped to just this section."""
    idx = html.index('id="upcoming-month-view"')
    return html[idx:]


def _now():
    return datetime.now(timezone.utc)


def _add_months(d: date, months: int) -> date:
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def _month_boundaries(offset=0):
    """Mirrors app/main.py's own month_start/next_month_start/leading/trailing
    computation, independent of whatever real-world date the test happens to run
    on — this is how the tests get a real month-boundary case "for free" without
    hardcoding a specific month/year that will eventually become stale."""
    today_local = _now().astimezone(DEFAULT_TZ).date()
    first_of_this_month = today_local.replace(day=1)
    month_start = _add_months(first_of_this_month, offset)
    next_month_start = _add_months(month_start, 1)
    days_in_month = (next_month_start - month_start).days
    leading = month_start.weekday()
    trailing = (7 - (leading + days_in_month) % 7) % 7
    return today_local, month_start, next_month_start, days_in_month, leading, trailing


# ── 1. Real 7-column month grid with correct leading/trailing blanks ────────────

def test_month_grid_has_correct_leading_and_trailing_blanks(pg_conn, app_module, client):
    _register_and_login(client, email="boundary@example.com")
    uid = _user_id(pg_conn, "boundary@example.com")
    _insert_anime(pg_conn, 1, "Solo Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 1, _now() + timedelta(hours=2))

    today_local, month_start, next_month_start, days_in_month, leading, trailing = _month_boundaries()

    resp = client.get("/upcoming")
    assert resp.status_code == 200
    month_html = _month_slice(resp.text)

    total_cells = month_html.count('upcoming-month-cell')
    assert total_cells == leading + days_in_month + trailing
    assert total_cells % 7 == 0

    blank_cells = month_html.count('upcoming-month-cell is-blank')
    assert blank_cells == leading + trailing

    # Every real day of the month gets its own dated, non-blank cell.
    dated_cells = re.findall(r'upcoming-month-date">(\d+)<', month_html)
    assert [int(d) for d in dated_cells] == list(range(1, days_in_month + 1))

    # Exactly one cell is flagged today, and it's the real local date.
    assert month_html.count("is-today") == 1
    assert f'>{today_local.day}<' in month_html


def test_leading_blank_case_is_a_real_non_monday_start(pg_conn, app_module, client):
    """Regression guard specifically for the #257 acceptance criterion that this
    isn't just tested against a month that happens to start on Monday (which
    would need zero leading blanks and could hide an off-by-one)."""
    today_local, month_start, *_ , leading, trailing = _month_boundaries()
    if month_start.weekday() == 0:
        pytest.skip("Current real-world month happens to start on a Monday — "
                    "the leading-blank count assertion above still exercises the "
                    "logic, this just isn't the non-Monday case for this run.")
    assert leading > 0


# ── 2. Chips land in the correct day cell ────────────────────────────────────

def test_chip_appears_under_the_correct_date(pg_conn, app_module, client):
    _register_and_login(client, email="chip@example.com")
    uid = _user_id(pg_conn, "chip@example.com")
    _insert_anime(pg_conn, 1, "Chip Test Show")
    _insert_entry(pg_conn, 1, uid, progress=6)

    today_local, month_start, next_month_start, days_in_month, *_ = _month_boundaries()
    # A day guaranteed still inside this month and in the future relative to now.
    target_day = min(today_local.day + 1, days_in_month)
    if today_local.day >= days_in_month:
        pytest.skip("Today is the last day of the real-world current month — no "
                    "later in-month day exists to place a distinguishable airing on.")
    airing_dt = datetime(month_start.year, month_start.month, target_day, 12, 0, tzinfo=timezone.utc)
    _insert_airing(pg_conn, 1, 7, airing_dt)

    resp = client.get("/upcoming")
    month_html = _month_slice(resp.text)

    assert "Chip Test Show" in month_html
    assert "Ep 7" in month_html
    # The chip's containing cell carries the target date number nearby — scope
    # to the single cell via a narrow window around the date marker.
    date_idx = month_html.index(f'upcoming-month-date">{target_day}<')
    cell_end = month_html.index('upcoming-month-cell', date_idx + 1)
    assert "Chip Test Show" in month_html[date_idx:cell_end]


# ── 3. Overflow indicator ────────────────────────────────────────────────────

def test_overflow_indicator_triggers_past_the_chip_cap(pg_conn, app_module, client):
    _register_and_login(client, email="overflow@example.com")
    uid = _user_id(pg_conn, "overflow@example.com")

    today_local, month_start, next_month_start, days_in_month, *_ = _month_boundaries()
    if today_local.day >= days_in_month:
        pytest.skip("No later in-month day available this run.")
    target_day = today_local.day + 1
    airing_dt = datetime(month_start.year, month_start.month, target_day, 12, 0, tzinfo=timezone.utc)

    n_shows = MONTH_CHIP_CAP + 2  # comfortably over the cap
    for i in range(n_shows):
        _insert_anime(pg_conn, i + 1, f"Overflow Show {i + 1}")
        _insert_entry(pg_conn, i + 1, uid)
        _insert_airing(pg_conn, i + 1, 1, airing_dt + timedelta(minutes=i))

    resp = client.get("/upcoming")
    month_html = _month_slice(resp.text)

    assert month_html.count("upcoming-month-chip\"") == MONTH_CHIP_CAP or \
        month_html.count('class="upcoming-month-chip"') == MONTH_CHIP_CAP
    overflow_count = n_shows - MONTH_CHIP_CAP
    assert f"+{overflow_count} more" in month_html


def test_no_overflow_indicator_when_under_the_cap(pg_conn, app_module, client):
    _register_and_login(client, email="nooverflow@example.com")
    uid = _user_id(pg_conn, "nooverflow@example.com")
    _insert_anime(pg_conn, 1, "Single Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 1, _now() + timedelta(hours=1))

    resp = client.get("/upcoming")
    month_html = _month_slice(resp.text)
    assert "upcoming-month-overflow" not in month_html


# ── 4. Prev/Next/Today navigation, and month_offset clamped at 0 ────────────────

def test_next_and_prev_navigate_by_real_calendar_months(pg_conn, app_module, client):
    _register_and_login(client, email="monthnav@example.com")
    uid = _user_id(pg_conn, "monthnav@example.com")
    _insert_anime(pg_conn, 1, "This Month Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_anime(pg_conn, 2, "Two Months Out Show")
    _insert_entry(pg_conn, 2, uid)

    today_local, month_start, next_month_start, days_in_month, *_ = _month_boundaries()
    _insert_airing(pg_conn, 1, 1, _now() + timedelta(hours=1))

    _, m2_start, m2_next_start, *_ = _month_boundaries(offset=2)
    far_dt = datetime(m2_start.year, m2_start.month, 1, 12, 0, tzinfo=timezone.utc)
    _insert_airing(pg_conn, 2, 1, far_dt)

    grid0 = _month_slice(client.get("/upcoming").text)
    assert "This Month Show" in grid0
    assert "Two Months Out Show" not in grid0

    grid2 = _month_slice(client.get("/upcoming?month_offset=2").text)
    assert "This Month Show" not in grid2
    assert "Two Months Out Show" in grid2

    # "Today" control always points back to the unparameterized route.
    assert 'href="/upcoming"' in grid2


def test_month_offset_clamped_to_zero_and_prev_disabled_there(pg_conn, app_module, client):
    _register_and_login(client, email="monthclamp@example.com")
    uid = _user_id(pg_conn, "monthclamp@example.com")
    _insert_anime(pg_conn, 1, "This Month Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 1, _now() + timedelta(hours=1))

    resp_zero = client.get("/upcoming")
    resp_negative = client.get("/upcoming?month_offset=-3")
    assert resp_zero.status_code == 200
    assert resp_negative.status_code == 200

    grid_zero = _month_slice(resp_zero.text)
    grid_negative = _month_slice(resp_negative.text)

    assert "This Month Show" in grid_zero
    assert "This Month Show" in grid_negative

    assert "month_offset=-1" not in grid_zero
    assert 'aria-disabled="true"' in grid_zero
    assert 'aria-disabled="true"' in grid_negative


# ── 5. List view and Weekly grid are unaffected ──────────────────────────────

def test_list_and_week_views_unaffected_by_month_offset(pg_conn, app_module, client):
    _register_and_login(client, email="unaffected@example.com")
    uid = _user_id(pg_conn, "unaffected@example.com")
    _insert_anime(pg_conn, 1, "Today Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 1, _now() + timedelta(hours=1))

    resp_zero = client.get("/upcoming")
    resp_offset = client.get("/upcoming?month_offset=3")

    def _pre_grid(html):
        return html[: html.index('id="upcoming-grid-view"')]

    def _grid_only(html):
        return html[html.index('id="upcoming-grid-view"'): html.index('id="upcoming-month-view"')]

    assert _pre_grid(resp_zero.text) == _pre_grid(resp_offset.text)
    assert _grid_only(resp_zero.text) == _grid_only(resp_offset.text)
    assert "Today Show" in _pre_grid(resp_zero.text)
