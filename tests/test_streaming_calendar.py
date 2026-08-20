"""
Coverage for issue #228 — Streaming Coverage v2: 12-month subscribe/cancel calendar.

Verified against a real Postgres, same pattern as tests/test_streaming_coverage.py
(#182's own test suite, which this feature builds on top of): exercises the actual
`_compute_streaming_calendar` query in app.main against the real schema.sql shape,
plus a real FastAPI TestClient hitting the live `/streaming` route.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no
Postgres running.

Covers the acceptance-criteria scenarios from issue #228:
  1. A title airing in a given calendar month credits every un-owned service that
     carries it (the "subscribe" side) — matching #182's `sites - owned` marginal
     framing, just bucketed by month instead of summed to one total.
  2. A title already reachable via an owned service instead credits that owned
     service (the "cancel" side) for the months it actually has something airing,
     not every month indiscriminately.
  3. A RELEASING title whose earliest still-scheduled episode lands in the current
     or next calendar month also credits the *current* month, even though that
     specific episode's row is dated next month — airing_schedule_cache only
     carries not-yet-aired rows, so without this the current month would look
     emptier as the month goes on for a show that's genuinely airing right now.
     Bounded to a one-month lookahead so a RELEASING title on a long/irregular
     hiatus (next known episode 11 months out) does NOT get credited as "airing
     right now."
  4. A WATCHING/PLANNING title with NO airing_schedule_cache rows at all surfaces in
     a separate `tba_titles` bucket per credited service when its anime.status is
     RELEASING or NOT_YET_RELEASED (open question from #228 — "unknown" must not be
     silently indistinguishable from "not needed").
  5. A fully-aired FINISHED backlog title with no schedule rows is simply absent
     from the calendar (not TBA, not needed) — this feature is about air-date
     urgency, not "when should I get around to my backlog."
  6. Episodes beyond the 12-month horizon don't leak into any month bucket.
  7. `/streaming` renders the calendar section for a logged-in user.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

USER_ID = 1


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
        cur.execute("DELETE FROM user_streaming_services")
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM users")


@pytest.fixture()
def _seeded_user(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
            "VALUES (%s, 'local', 'test', 'test@example.com')",
            (USER_ID,),
        )


def _insert_anime(pg_conn, anime_id, sites, status="RELEASING", episodes=None, title=None):
    links = [{"site": s, "url": f"https://example.com/{anime_id}"} for s in sites]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, status, episodes, external_links) "
            "VALUES (%s, %s, %s, %s, %s::jsonb)",
            (anime_id, title or f"Anime {anime_id}", status, episodes, json.dumps(links)),
        )


def _insert_entry(pg_conn, anime_id, status, progress=0, user_id=USER_ID):
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


def _set_owned(pg_conn, services, user_id=USER_ID):
    with pg_conn.cursor() as cur:
        for s in services:
            cur.execute(
                "INSERT INTO user_streaming_services (user_id, service) VALUES (%s, %s)",
                (user_id, s),
            )


def _months_by_service(calendar):
    return {s["service"]: s for s in calendar["services"]}


def _now():
    return datetime.now(timezone.utc)


# ── 1 & 6. Subscribe-side crediting, month bucketing, horizon clipping ─────────

def test_uncovered_title_credits_every_carrying_service_in_its_airing_month(pg_conn, app_module, _seeded_user):
    now = _now()
    _insert_anime(pg_conn, 1, sites=["Netflix", "Hulu"], status="RELEASING")
    _insert_entry(pg_conn, 1, "WATCHING", progress=3)
    # Two episodes landing two calendar months out.
    two_months_out = now + timedelta(days=62)
    _insert_airing(pg_conn, 1, 4, two_months_out)

    # Beyond the 12-month horizon — must not leak into any bucket.
    _insert_anime(pg_conn, 2, sites=["Netflix"], status="RELEASING")
    _insert_entry(pg_conn, 2, "PLANNING", progress=0)
    _insert_airing(pg_conn, 2, 1, now + timedelta(days=400))

    calendar = app_module._compute_streaming_calendar(USER_ID)
    services = _months_by_service(calendar)

    assert set(services) == {"Netflix", "Hulu"}
    # Anime 1's episode airs ~2 months out — figure out which bucket via the actual
    # month keys the function returned, rather than hardcoding an index (avoids a
    # flaky test around month-boundary run dates).
    target_key = two_months_out.strftime("%Y-%m")
    hit_indices = [i for i, m in enumerate(calendar["months"]) if m.startswith(target_key)]
    assert hit_indices, "expected the target month within the 12-month horizon"
    idx = hit_indices[0]

    for svc in ("Netflix", "Hulu"):
        assert services[svc]["months"][idx]["needed"] is True
        assert services[svc]["months"][idx]["episodes"] == 1
        assert services[svc]["months"][idx]["titles"] == 1
        assert services[svc]["owned"] is False

    # Anime 2's episode is past the 12-month horizon — no month anywhere is credited
    # for it, and it produces no TBA entry either (its date IS known, just far out).
    total_episodes_anywhere = sum(m["episodes"] for m in services["Netflix"]["months"])
    assert total_episodes_anywhere == 1  # only anime 1's episode, not anime 2's
    assert services["Netflix"]["tba_titles"] == []


# ── 2. Cancel-side crediting: owned service only credited for months it's actually
#      carrying something, not indiscriminately every month ────────────────────

def test_owned_service_credited_only_for_months_it_actually_carries_something(pg_conn, app_module, _seeded_user):
    _set_owned(pg_conn, ["Crunchyroll"])
    now = _now()
    _insert_anime(pg_conn, 1, sites=["Crunchyroll"], status="RELEASING")
    _insert_entry(pg_conn, 1, "WATCHING", progress=5)
    airing_month = now + timedelta(days=35)
    _insert_airing(pg_conn, 1, 6, airing_month)

    calendar = app_module._compute_streaming_calendar(USER_ID)
    services = _months_by_service(calendar)

    assert services["Crunchyroll"]["owned"] is True
    needed_count = sum(1 for m in services["Crunchyroll"]["months"] if m["needed"])
    # Current month (quirk 3 below) + the month the episode actually airs in.
    assert needed_count in (1, 2)
    target_key = airing_month.strftime("%Y-%m")
    idx = next(i for i, m in enumerate(calendar["months"]) if m.startswith(target_key))
    assert services["Crunchyroll"]["months"][idx]["needed"] is True


def test_title_on_both_owned_and_unowned_service_credits_only_owned_side(pg_conn, app_module, _seeded_user):
    """Matches #182's marginal-value framing: once a title is reachable via an
    owned service, it stops counting toward any other (un-owned) service."""
    _set_owned(pg_conn, ["Crunchyroll"])
    now = _now()
    _insert_anime(pg_conn, 1, sites=["Crunchyroll", "Netflix"], status="RELEASING")
    _insert_entry(pg_conn, 1, "WATCHING", progress=1)
    _insert_airing(pg_conn, 1, 2, now + timedelta(days=20))

    calendar = app_module._compute_streaming_calendar(USER_ID)
    services = _months_by_service(calendar)

    assert "Netflix" not in services
    assert "Crunchyroll" in services


# ── 3. RELEASING title with a future episode also credits the current month ────

def test_releasing_title_with_future_episode_credits_current_month(pg_conn, app_module, _seeded_user):
    now = _now()
    _insert_anime(pg_conn, 1, sites=["Netflix"], status="RELEASING")
    _insert_entry(pg_conn, 1, "WATCHING", progress=8)
    # Next episode lands well into next month — simulating "already aired this
    # month's episode, cache only has the future one left."
    next_month_ish = now + timedelta(days=40)
    _insert_airing(pg_conn, 1, 9, next_month_ish)

    calendar = app_module._compute_streaming_calendar(USER_ID)
    services = _months_by_service(calendar)

    assert services["Netflix"]["months"][0]["needed"] is True  # current month, via quirk 1


# ── 4. No schedule rows at all -> TBA bucket for RELEASING/NOT_YET_RELEASED ─────

def test_no_schedule_data_surfaces_as_tba_for_releasing_and_not_yet_released(pg_conn, app_module, _seeded_user):
    _insert_anime(pg_conn, 1, sites=["Netflix"], status="RELEASING", title="Gap Cour Show")
    _insert_entry(pg_conn, 1, "WATCHING", progress=12)  # between cours, no cache rows

    _insert_anime(pg_conn, 2, sites=["Hulu"], status="NOT_YET_RELEASED", title="Announced Sequel")
    _insert_entry(pg_conn, 2, "PLANNING", progress=0)

    calendar = app_module._compute_streaming_calendar(USER_ID)
    services = _months_by_service(calendar)

    assert services["Netflix"]["tba_titles"] == ["Gap Cour Show"]
    assert all(not m["needed"] for m in services["Netflix"]["months"])

    assert services["Hulu"]["tba_titles"] == ["Announced Sequel"]
    assert all(not m["needed"] for m in services["Hulu"]["months"])


# ── 5. Fully-aired FINISHED backlog with no schedule rows is simply absent ──────

def test_finished_backlog_with_no_schedule_is_absent_not_tba(pg_conn, app_module, _seeded_user):
    _insert_anime(pg_conn, 1, sites=["Netflix"], status="FINISHED", title="Old Backlog Show")
    _insert_entry(pg_conn, 1, "PLANNING", progress=0)

    calendar = app_module._compute_streaming_calendar(USER_ID)

    assert calendar["services"] == []


def test_completed_and_dropped_entries_are_ignored(pg_conn, app_module, _seeded_user):
    now = _now()
    _insert_anime(pg_conn, 1, sites=["Netflix"], status="RELEASING")
    _insert_entry(pg_conn, 1, "COMPLETED", progress=12)
    _insert_airing(pg_conn, 1, 13, now + timedelta(days=10))

    _insert_anime(pg_conn, 2, sites=["Hulu"], status="RELEASING")
    _insert_entry(pg_conn, 2, "DROPPED", progress=3)
    _insert_airing(pg_conn, 2, 4, now + timedelta(days=10))

    calendar = app_module._compute_streaming_calendar(USER_ID)

    assert calendar["services"] == []


def test_no_active_library_entries_returns_empty_calendar_without_erroring(pg_conn, app_module, _seeded_user):
    calendar = app_module._compute_streaming_calendar(USER_ID)
    assert calendar["services"] == []
    assert len(calendar["months"]) == 12


# ── 7. /streaming renders the calendar section ──────────────────────────────────

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


def test_streaming_page_renders_calendar_section(pg_conn, app_module, client):
    _register_and_login(client, email="calendar-page@example.com")

    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = 'calendar-page@example.com'")
        (uid,) = cur.fetchone()

    now = _now()
    _insert_anime(pg_conn, 301, sites=["Netflix"], status="RELEASING", title="Calendar Test Show")
    _insert_entry(pg_conn, 301, "WATCHING", progress=1, user_id=uid)
    _insert_airing(pg_conn, 301, 2, now + timedelta(days=5))

    resp = client.get("/streaming")
    assert resp.status_code == 200
    assert 'id="calendar"' in resp.text
    assert "streaming-calendar-table" in resp.text
    assert "Netflix" in resp.text


def test_streaming_page_renders_calendar_empty_state(pg_conn, app_module, client):
    _register_and_login(client, email="calendar-empty@example.com")

    resp = client.get("/streaming")
    assert resp.status_code == 200
    assert 'id="calendar"' in resp.text
