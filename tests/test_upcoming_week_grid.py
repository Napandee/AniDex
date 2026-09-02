"""
Coverage for issue #256 — /upcoming's "Weekly grid" view used to have no upper date
bound at all: the query pulled every future `airing_schedule_cache` row and bucketed
ALL of them by `.weekday()`, so a weekly show with N future episodes cached showed up
N times stacked in the same weekday column instead of the grid showing one real week.
(Real-world trigger: "Skeleton Knight In Another World Season 2" appeared five times
under the same Monday column, episodes 8-12 spanning five different weeks.)

Verified against a real Postgres, same pattern as tests/test_streaming_calendar.py —
skipped entirely if one isn't reachable via DATABASE_URL.

Covers the acceptance criteria from #256:
  1. A weekly show with multiple future occurrences appears at most once in the grid
     at a time, not once per future week simultaneously.
  2. Prev/Next navigation (`?week_offset=N`) moves by real calendar weeks — an entry
     exactly 7 days out from another only ever appears in the *other* week's grid.
  3. The week is a real Monday-Sunday calendar week: all 7 day columns always render
     (even ones earlier in the week than "today"), each with its own header showing
     the actual date, not just a bare weekday name.
  4. `week_offset` is clamped at 0 server-side — airing_schedule_cache only ever holds
     not-yet-aired rows, so any week before the current one is guaranteed empty; Prev
     is rendered as disabled there instead of linking to a knowably-empty week.
  5. The List view (`entries`, driving the Today/Tomorrow/etc. grouping) is completely
     unaffected — same entries, same grouping, regardless of week_offset.
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


def _grid_slice(html):
    """Only the weekly-grid section of the rendered page (list view before it, and
    the Month calendar view added after it in #257, are both stripped out) — the
    same episode text legitimately appears in the List view and the Month view
    too, so assertions about the weekly grid must be scoped to just this section."""
    start = html.index('id="upcoming-grid-view"')
    end = html.index('id="upcoming-month-view"')
    return html[start:end]


def _now():
    return datetime.now(timezone.utc)


def _safely_future(hours=2):
    """A timestamp guaranteed to be in the future and, for any normal test run,
    still within 'today' in DEFAULT_TZ (only risks crossing midnight local time in
    the last couple of hours of the local day, an acceptable rare flake window)."""
    return _now() + timedelta(hours=hours)


# ── 1. A weekly show with N future occurrences shows up at most once ────────────

def test_weekly_show_appears_at_most_once_in_current_week_grid(pg_conn, app_module, client):
    _register_and_login(client, email="weekly@example.com")
    uid = _user_id(pg_conn, "weekly@example.com")

    _insert_anime(pg_conn, 1, "Skeleton Knight In Another World Season 2", episodes=24)
    _insert_entry(pg_conn, 1, uid, status="WATCHING", progress=7)

    this_week_dt = _safely_future()
    # Same weekly show, 5 future occurrences a week apart each — exactly the bug
    # scenario from the issue (episodes 8-12 spanning 5 distinct future weeks).
    for i, ep in enumerate(range(8, 13)):
        _insert_airing(pg_conn, 1, ep, this_week_dt + timedelta(weeks=i))

    resp = client.get("/upcoming")
    assert resp.status_code == 200
    grid_html = _grid_slice(resp.text)

    assert grid_html.count("Episode 8") == 1
    for ep in (9, 10, 11, 12):
        assert f"Episode {ep}" not in grid_html, (
            f"Episode {ep} (a future week's occurrence) leaked into the current week's grid"
        )

    # List view is untouched — all 5 occurrences still present there.
    list_html = resp.text[: resp.text.index('id="upcoming-grid-view"')]
    for ep in range(8, 13):
        assert f"Episode {ep}" in list_html


# ── 2. Prev/Next moves by real calendar weeks, not a rolling window ─────────────

def test_next_and_prev_move_by_real_calendar_weeks(pg_conn, app_module, client):
    _register_and_login(client, email="nav@example.com")
    uid = _user_id(pg_conn, "nav@example.com")

    _insert_anime(pg_conn, 1, "This Week Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_anime(pg_conn, 2, "Next Week Show")
    _insert_entry(pg_conn, 2, uid)
    _insert_anime(pg_conn, 3, "Two Weeks Out Show")
    _insert_entry(pg_conn, 3, uid)

    this_week_dt = _safely_future()
    _insert_airing(pg_conn, 1, 1, this_week_dt)
    _insert_airing(pg_conn, 2, 1, this_week_dt + timedelta(weeks=1))
    _insert_airing(pg_conn, 3, 1, this_week_dt + timedelta(weeks=2))

    grid0 = _grid_slice(client.get("/upcoming").text)
    assert "This Week Show" in grid0
    assert "Next Week Show" not in grid0
    assert "Two Weeks Out Show" not in grid0

    grid1 = _grid_slice(client.get("/upcoming?week_offset=1").text)
    assert "This Week Show" not in grid1
    assert "Next Week Show" in grid1
    assert "Two Weeks Out Show" not in grid1

    grid2 = _grid_slice(client.get("/upcoming?week_offset=2").text)
    assert "This Week Show" not in grid2
    assert "Next Week Show" not in grid2
    assert "Two Weeks Out Show" in grid2

    # "Today" control always points back to the unparameterized route.
    assert 'href="/upcoming"' in grid1


def test_week_offset_clamped_to_zero_and_prev_disabled_there(pg_conn, app_module, client):
    _register_and_login(client, email="clamp@example.com")
    uid = _user_id(pg_conn, "clamp@example.com")

    _insert_anime(pg_conn, 1, "This Week Show")
    _insert_entry(pg_conn, 1, uid)
    this_week_dt = _safely_future()
    _insert_airing(pg_conn, 1, 1, this_week_dt)

    resp_zero = client.get("/upcoming")
    resp_negative = client.get("/upcoming?week_offset=-5")
    assert resp_zero.status_code == 200
    assert resp_negative.status_code == 200

    grid_zero = _grid_slice(resp_zero.text)
    grid_negative = _grid_slice(resp_negative.text)

    # Negative offset clamps to the same current week — same content either way.
    assert "This Week Show" in grid_zero
    assert "This Week Show" in grid_negative

    # Prev is rendered disabled (no live link to week_offset=-1) at the floor.
    assert "week_offset=-1" not in grid_zero
    assert 'aria-disabled="true"' in grid_zero
    assert 'aria-disabled="true"' in grid_negative


# ── 3. Real Mon-Sun week: all 7 columns render with dated headers ───────────────

def test_grid_always_renders_seven_dated_day_columns(pg_conn, app_module, client):
    _register_and_login(client, email="columns@example.com")
    uid = _user_id(pg_conn, "columns@example.com")

    _insert_anime(pg_conn, 1, "Solo Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 1, _safely_future())

    resp = client.get("/upcoming")
    grid_html = _grid_slice(resp.text)

    assert grid_html.count("upcoming-grid-day-header") == 7
    # Each header shows a weekday name AND an actual date, e.g. "Monday, Aug 25".
    headers = re.findall(r'upcoming-grid-day-header">([^<]+)<', grid_html)
    assert len(headers) == 7
    weekday_re = re.compile(
        r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
        r"[A-Za-z]{3} \d{2}$"
    )
    for h in headers:
        assert weekday_re.match(h), f"header {h!r} missing weekday name + date"

    # Exactly one column is flagged as today, and its date matches the real local date.
    assert grid_html.count("is-today") == 1
    today_local = datetime.now(timezone.utc).astimezone(DEFAULT_TZ).date()
    assert today_local.strftime("%b %d") in grid_html


def test_mid_week_today_still_shows_earlier_days_in_same_week(pg_conn, app_module, client):
    """Regression guard for the resolved-in-issue decision: the grid boundary is a
    real calendar week (Mon-Sun), not a rolling 'next 7 days from today' — so a
    Wednesday 'today' still renders Monday/Tuesday's columns (just empty, since
    airing_schedule_cache never holds already-aired rows)."""
    _register_and_login(client, email="midweek@example.com")
    uid = _user_id(pg_conn, "midweek@example.com")
    _insert_anime(pg_conn, 1, "Solo Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 1, _safely_future())

    resp = client.get("/upcoming")
    grid_html = _grid_slice(resp.text)

    today_local = datetime.now(timezone.utc).astimezone(DEFAULT_TZ).date()
    monday_this_week = today_local - timedelta(days=today_local.weekday())
    headers = re.findall(r'upcoming-grid-day-header">([^<]+)<', grid_html)
    expected_dates = [(monday_this_week + timedelta(days=i)).strftime("%b %d") for i in range(7)]
    for expected in expected_dates:
        assert any(expected in h for h in headers), (
            f"expected column for {expected} missing — earlier days in the current "
            "week must still render even when 'today' is mid-week"
        )


# ── 5. List view (entries / grouping) is unaffected by any of this ──────────────

def test_list_view_grouping_unaffected_by_week_offset(pg_conn, app_module, client):
    _register_and_login(client, email="listview@example.com")
    uid = _user_id(pg_conn, "listview@example.com")
    _insert_anime(pg_conn, 1, "Today Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 1, _now() + timedelta(hours=1))

    resp_zero = client.get("/upcoming")
    resp_offset = client.get("/upcoming?week_offset=3")

    list_zero = resp_zero.text[: resp_zero.text.index('id="upcoming-grid-view"')]
    list_offset = resp_offset.text[: resp_offset.text.index('id="upcoming-grid-view"')]

    assert "Today Show" in list_zero
    assert "Today Show" in list_offset
    assert list_zero == list_offset
