"""
Coverage for issue #338 — "On this day" watch-anniversary card on /upcoming,
computed purely from library_entries.start_date/finish_date (no new stored
state, matching #164's own "compute on the fly" precedent for the sibling
pace-stat feature). See _on_this_day_anniversaries()'s docstring in
app/main.py for the exact-day-match / years_ago >= 1 / same-day-combining
rules this test verifies.

Same real-Postgres TestClient pattern as tests/test_upcoming_day_filter.py —
skipped entirely if Postgres isn't reachable via DATABASE_URL. Default
timezone (Europe/London, app/config.py's DEFAULT_SETTINGS) is what a freshly
registered user gets, so "today" here matches _on_this_day_anniversaries'
own timezone-local computation without needing a settings override.

Covers the acceptance criteria from #338:
  1. A start_date/finish_date matching today's month/day in a prior year
     surfaces as an anniversary, with the correct years-ago count.
  2. A start_date/finish_date matching today's month/day in the CURRENT year
     (years_ago == 0) is not shown — that's just "today", not an anniversary.
  3. A start_date that does not match today's month/day is not shown.
  4. start_date == finish_date on the same matching day renders one combined
     "started and finished" event, not two.
  5. The card only ever appears on the default view — never while
     week_offset/month_offset have moved the page, or while a date_filter has
     narrowed the list to a different day.
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


DEFAULT_TZ = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM users")


def _register_and_login(client, email="owner@example.com", password="correct horse battery staple"):
    resp = client.post(
        "/auth/register", data={"email": email, "password": password}, follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def _user_id(pg_conn, email):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        return cur.fetchone()[0]


def _insert_anime(pg_conn, anime_id, title):
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO anime (id, title_romaji) VALUES (%s, %s)", (anime_id, title))


def _insert_entry(pg_conn, anime_id, user_id, start_date=None, finish_date=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, start_date, finish_date) "
            "VALUES (%s, %s, 'COMPLETED', %s, %s)",
            (user_id, anime_id, start_date, finish_date),
        )


def _today_local():
    return datetime.now(timezone.utc).astimezone(DEFAULT_TZ).date()


def _years_ago(years: int) -> date:
    today = _today_local()
    if today.month == 2 and today.day == 29:
        pytest.skip("Today is Feb 29 — no clean N-years-ago same-month-day date exists this run.")
    return date(today.year - years, today.month, today.day)


ONTHISDAY_CARD_MARKER = 'class="panel onthisday-card"'


# ── 1/2/3. Match/no-match, years_ago gating ─────────────────────────────────

def test_prior_year_start_date_surfaces_as_anniversary(pg_conn, client):
    _register_and_login(client, email="a@example.com")
    uid = _user_id(pg_conn, "a@example.com")
    _insert_anime(pg_conn, 1, "Three Years Ago Show")
    _insert_entry(pg_conn, 1, uid, start_date=_years_ago(3))

    resp = client.get("/upcoming")
    assert resp.status_code == 200
    assert ONTHISDAY_CARD_MARKER in resp.text
    assert "Three Years Ago Show" in resp.text
    assert "3" in resp.text.split(ONTHISDAY_CARD_MARKER)[1][:600]


def test_same_year_start_date_is_not_an_anniversary(pg_conn, client):
    """years_ago == 0 (started earlier today, in the current year) is just
    "today" — not rendered as a 0-year anniversary."""
    _register_and_login(client, email="b@example.com")
    uid = _user_id(pg_conn, "b@example.com")
    _insert_anime(pg_conn, 2, "Started Today Show")
    _insert_entry(pg_conn, 2, uid, start_date=_today_local())

    resp = client.get("/upcoming")
    assert resp.status_code == 200
    assert ONTHISDAY_CARD_MARKER not in resp.text


def test_non_matching_date_is_not_shown(pg_conn, client):
    _register_and_login(client, email="c@example.com")
    uid = _user_id(pg_conn, "c@example.com")
    _insert_anime(pg_conn, 3, "Unrelated Date Show")
    off_date = _years_ago(2) - timedelta(days=45)
    _insert_entry(pg_conn, 3, uid, start_date=off_date)

    resp = client.get("/upcoming")
    assert resp.status_code == 200
    assert "Unrelated Date Show" not in resp.text


# ── 4. Same-day start+finish combines into one event ────────────────────────

def test_same_day_start_and_finish_combines_into_one_event(pg_conn, client):
    _register_and_login(client, email="d@example.com")
    uid = _user_id(pg_conn, "d@example.com")
    same_day = _years_ago(1)
    _insert_anime(pg_conn, 4, "Movie Watched In One Sitting")
    _insert_entry(pg_conn, 4, uid, start_date=same_day, finish_date=same_day)

    resp = client.get("/upcoming")
    assert resp.status_code == 200
    assert resp.text.count("Movie Watched In One Sitting") == 1


# ── 5. Only rendered on the default view ─────────────────────────────────────

def test_card_absent_when_week_offset_set(pg_conn, client):
    _register_and_login(client, email="e@example.com")
    uid = _user_id(pg_conn, "e@example.com")
    _insert_anime(pg_conn, 5, "Anniversary Show")
    _insert_entry(pg_conn, 5, uid, start_date=_years_ago(2))

    resp = client.get("/upcoming?week_offset=1")
    assert resp.status_code == 200
    assert ONTHISDAY_CARD_MARKER not in resp.text


def test_card_absent_when_date_filter_set(pg_conn, client):
    _register_and_login(client, email="f@example.com")
    uid = _user_id(pg_conn, "f@example.com")
    _insert_anime(pg_conn, 6, "Anniversary Show 2")
    _insert_entry(pg_conn, 6, uid, start_date=_years_ago(2))

    other_day = (_today_local() + timedelta(days=1)).isoformat()
    resp = client.get(f"/upcoming?date={other_day}")
    assert resp.status_code == 200
    assert ONTHISDAY_CARD_MARKER not in resp.text


def test_card_present_on_plain_default_view(pg_conn, client):
    _register_and_login(client, email="g@example.com")
    uid = _user_id(pg_conn, "g@example.com")
    _insert_anime(pg_conn, 7, "Anniversary Show 3")
    _insert_entry(pg_conn, 7, uid, start_date=_years_ago(4))

    resp = client.get("/upcoming?week_offset=0&month_offset=0")
    assert resp.status_code == 200
    assert ONTHISDAY_CARD_MARKER in resp.text
