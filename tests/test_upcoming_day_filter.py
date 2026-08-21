"""
Coverage for issue #277 — clicking a day cell (or its "+N more" overflow text) in
the /upcoming month grid navigates to the existing list view filtered down to that
single date, reusing the list-item card markup rather than a new modal/popover.

Verified against a real Postgres, same pattern as tests/test_upcoming_month_grid.py —
skipped entirely if one isn't reachable via DATABASE_URL.

Covers the acceptance criteria from #277:
  1. `?date=YYYY-MM-DD` narrows the rendered entries to just that day's airings,
     using the same list-item card markup as the plain List view.
  2. Each in-month day cell's date number, and the "+N more" overflow text, link to
     `/upcoming?date=<that day>&month_offset=<current offset>`.
  3. The filtered day view carries a link back to the month view at the same
     `month_offset` it came from.
  4. No date filter (the plain `/upcoming` route) renders exactly as before —
     list/week/month toggle behavior is unaffected.
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
    today_local = _now().astimezone(DEFAULT_TZ).date()
    first_of_this_month = today_local.replace(day=1)
    month_start = _add_months(first_of_this_month, offset)
    next_month_start = _add_months(month_start, 1)
    days_in_month = (next_month_start - month_start).days
    return today_local, month_start, next_month_start, days_in_month


# ── 1. Date filter narrows results correctly ─────────────────────────────────

def test_date_filter_shows_only_that_days_entries(pg_conn, app_module, client):
    _register_and_login(client, email="filter@example.com")
    uid = _user_id(pg_conn, "filter@example.com")
    _insert_anime(pg_conn, 1, "Target Day Show")
    _insert_entry(pg_conn, 1, uid, progress=4)
    _insert_anime(pg_conn, 2, "Other Day Show")
    _insert_entry(pg_conn, 2, uid, progress=1)

    today_local, month_start, next_month_start, days_in_month = _month_boundaries()
    if today_local.day >= days_in_month:
        pytest.skip("Today is the last day of the real-world current month — no "
                    "later in-month day exists to place a distinguishable airing on.")
    target_day = today_local.day + 1
    other_day = min(target_day + 1, days_in_month)
    if other_day == target_day:
        pytest.skip("No third distinguishable in-month day available this run.")

    target_dt = datetime(month_start.year, month_start.month, target_day, 12, 0, tzinfo=timezone.utc)
    other_dt = datetime(month_start.year, month_start.month, other_day, 12, 0, tzinfo=timezone.utc)
    _insert_airing(pg_conn, 1, 5, target_dt)
    _insert_airing(pg_conn, 2, 2, other_dt)

    target_date_str = date(month_start.year, month_start.month, target_day).isoformat()
    resp = client.get(f"/upcoming?date={target_date_str}")
    assert resp.status_code == 200

    assert "Target Day Show" in resp.text
    assert "Other Day Show" not in resp.text
    # Reuses the same list-item card markup as the plain List view.
    assert 'class="upcoming-item"' in resp.text
    assert 'class="upcoming-list"' in resp.text


def test_date_filter_with_no_entries_shows_empty_state(pg_conn, app_module, client):
    _register_and_login(client, email="emptyday@example.com")
    uid = _user_id(pg_conn, "emptyday@example.com")
    _insert_anime(pg_conn, 1, "Some Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 1, _now() + timedelta(days=10))

    # A date far enough in the future that nothing airs on it.
    far_date = (_now() + timedelta(days=400)).date().isoformat()
    resp = client.get(f"/upcoming?date={far_date}")
    assert resp.status_code == 200
    assert "Some Show" not in resp.text


def test_invalid_date_falls_back_to_unfiltered_view(pg_conn, app_module, client):
    _register_and_login(client, email="baddate@example.com")
    uid = _user_id(pg_conn, "baddate@example.com")
    _insert_anime(pg_conn, 1, "Fallback Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 1, _now() + timedelta(hours=1))

    resp = client.get("/upcoming?date=not-a-date")
    assert resp.status_code == 200
    assert "Fallback Show" in resp.text
    # Falls back to the normal toggled list/grid/month page, not the day view.
    assert 'id="upcoming-list-view"' in resp.text


# ── 2. Day-cell and overflow links point at the right filtered URL ──────────

def test_day_cell_date_links_to_filtered_view(pg_conn, app_module, client):
    _register_and_login(client, email="daylink@example.com")
    uid = _user_id(pg_conn, "daylink@example.com")
    _insert_anime(pg_conn, 1, "Linked Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 1, _now() + timedelta(hours=2))

    today_local, month_start, *_ = _month_boundaries()
    resp = client.get("/upcoming")
    month_html = _month_slice(resp.text)

    expected_date_str = today_local.isoformat()
    assert f'href="/upcoming?date={expected_date_str}&amp;month_offset=0"' in month_html \
        or f'href="/upcoming?date={expected_date_str}&month_offset=0"' in month_html


def test_overflow_text_links_to_same_filtered_view_as_its_day(pg_conn, app_module, client):
    _register_and_login(client, email="overflowlink@example.com")
    uid = _user_id(pg_conn, "overflowlink@example.com")

    today_local, month_start, next_month_start, days_in_month = _month_boundaries()
    if today_local.day >= days_in_month:
        pytest.skip("No later in-month day available this run.")
    target_day = today_local.day + 1
    airing_dt = datetime(month_start.year, month_start.month, target_day, 12, 0, tzinfo=timezone.utc)

    n_shows = MONTH_CHIP_CAP + 2
    for i in range(n_shows):
        _insert_anime(pg_conn, i + 1, f"Overflow Link Show {i + 1}")
        _insert_entry(pg_conn, i + 1, uid)
        _insert_airing(pg_conn, i + 1, 1, airing_dt + timedelta(minutes=i))

    resp = client.get("/upcoming?month_offset=0")
    month_html = _month_slice(resp.text)

    target_date_str = date(month_start.year, month_start.month, target_day).isoformat()
    overflow_idx = month_html.index("upcoming-month-overflow")
    # The overflow link's href carries the same date as the day's date-number link.
    window = month_html[max(0, overflow_idx - 400):overflow_idx + 100]
    assert f"date={target_date_str}" in window
    assert "month_offset=0" in window


def test_day_cell_link_carries_nonzero_month_offset(pg_conn, app_module, client):
    _register_and_login(client, email="offsetlink@example.com")
    uid = _user_id(pg_conn, "offsetlink@example.com")
    _insert_anime(pg_conn, 1, "Future Month Show")
    _insert_entry(pg_conn, 1, uid)

    _, m1_start, m1_next, days_in_m1 = _month_boundaries(offset=1)
    airing_dt = datetime(m1_start.year, m1_start.month, 1, 12, 0, tzinfo=timezone.utc)
    _insert_airing(pg_conn, 1, 1, airing_dt)

    resp = client.get("/upcoming?month_offset=1")
    month_html = _month_slice(resp.text)

    target_date_str = m1_start.isoformat()
    assert f"date={target_date_str}" in month_html
    assert "month_offset=1" in month_html


# ── 3. Back-to-month link preserves month_offset ─────────────────────────────

def test_filtered_view_links_back_to_month_at_same_offset(pg_conn, app_module, client):
    _register_and_login(client, email="backlink@example.com")
    uid = _user_id(pg_conn, "backlink@example.com")
    _insert_anime(pg_conn, 1, "Back Link Show")
    _insert_entry(pg_conn, 1, uid)

    _, m2_start, m2_next, days_in_m2 = _month_boundaries(offset=2)
    airing_dt = datetime(m2_start.year, m2_start.month, 1, 12, 0, tzinfo=timezone.utc)
    _insert_airing(pg_conn, 1, 1, airing_dt)

    target_date_str = m2_start.isoformat()
    resp = client.get(f"/upcoming?date={target_date_str}&month_offset=2")
    assert resp.status_code == 200
    assert "Back Link Show" in resp.text
    assert 'href="/upcoming?month_offset=2"' in resp.text


def test_filtered_view_back_link_defaults_to_month_offset_zero(pg_conn, app_module, client):
    _register_and_login(client, email="backdefault@example.com")
    uid = _user_id(pg_conn, "backdefault@example.com")
    _insert_anime(pg_conn, 1, "Today Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 1, _now() + timedelta(hours=1))

    today_local, *_ = _month_boundaries()
    resp = client.get(f"/upcoming?date={today_local.isoformat()}")
    assert resp.status_code == 200
    assert 'href="/upcoming?month_offset=0"' in resp.text


# ── 4. Plain /upcoming (no date filter) is unaffected ────────────────────────

def test_plain_upcoming_still_renders_full_toggle_ui(pg_conn, app_module, client):
    _register_and_login(client, email="unaffected2@example.com")
    uid = _user_id(pg_conn, "unaffected2@example.com")
    _insert_anime(pg_conn, 1, "Toggle Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 1, _now() + timedelta(hours=1))

    resp = client.get("/upcoming")
    assert resp.status_code == 200
    assert 'id="upcoming-list-view"' in resp.text
    assert 'id="upcoming-grid-view"' in resp.text
    assert 'id="upcoming-month-view"' in resp.text
    assert 'id="upcoming-view-list-btn"' in resp.text
    assert 'id="upcoming-view-month-btn"' in resp.text
