"""
Coverage for three brainstormed features greenlit and implemented together
(2026-08-27), bundled deliberately since all three extend the same shared
scheduler/notification-dispatcher machinery:

  - #372 Scheduled automatic backups — a cron job persisting the same
    full-instance export admin_export_all() builds on demand (issue #90),
    stored in Postgres (instance_backups) with retention pruning.
  - #374 Airing schedule change alerts — a before/after diff around the
    existing hourly airing_schedule_cache refresh, notifying watchers when a
    tracked title's air date shifts by at least _AIRING_SHIFT_THRESHOLD.
  - #375 Monthly recap digest — a first-of-month job summarizing last month's
    completions per user, reusing the same finish_date-scoped pattern
    stats_data()'s by_year/genre breakdowns already use.

Real Postgres, same pattern as tests/test_planning_coverage_notify.py:
app.main.notify is monkeypatched to capture calls instead of hitting a real
channel. Skipped entirely if no Postgres is reachable via DATABASE_URL.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

USER_ID = 1
OTHER_USER_ID = 2
ANIME_A = 951
ANIME_B = 952


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
        for uid in (USER_ID, OTHER_USER_ID):
            cur.execute(
                "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
                "VALUES (%s, 'local', %s, %s)",
                (uid, f"test{uid}", f"test{uid}@example.com"),
            )
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM instance_backups")
        cur.execute("DELETE FROM airing_schedule_cache")
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")


@pytest.fixture()
def app_module(pg_conn, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    return m


@pytest.fixture()
def sent(app_module, monkeypatch):
    captured = []
    monkeypatch.setattr(app_module, "notify", lambda user_id, title, body: captured.append((user_id, title, body)))
    return captured


def _insert_anime(pg_conn, anime_id, episodes=12, genres=None, title="Test Anime"):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, episodes, genres) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET episodes = EXCLUDED.episodes, genres = EXCLUDED.genres",
            (anime_id, title, episodes, json.dumps(genres or [])),
        )


def _insert_library_entry(pg_conn, user_id, anime_id, status, finish_date=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, finish_date) VALUES (%s, %s, %s, %s)",
            (user_id, anime_id, status, finish_date),
        )


# ── #372 — scheduled backups ────────────────────────────────────────────────


def test_scheduled_backup_creates_a_row_with_real_content(pg_conn, app_module):
    _insert_anime(pg_conn, ANIME_A, title="Frieren")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "COMPLETED", date(2026, 6, 1))

    app_module._scheduled_instance_backup()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT size_bytes, user_count, content FROM instance_backups")
        row = cur.fetchone()
    assert row is not None
    size_bytes, user_count, content = row
    assert size_bytes == len(bytes(content))
    assert user_count == 2  # USER_ID + OTHER_USER_ID both exist
    assert bytes(content)[:2] == b"PK"  # zip magic bytes


def test_scheduled_backup_prunes_to_retention_count(pg_conn, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_BACKUP_RETENTION_COUNT", 3)
    for _ in range(5):
        app_module._scheduled_instance_backup()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM instance_backups")
        (count,) = cur.fetchone()
    assert count == 3


# ── #374 — airing schedule change alerts ────────────────────────────────────


def test_shift_above_threshold_notifies_watcher(pg_conn, app_module, sent):
    _insert_anime(pg_conn, ANIME_A, title="Frieren")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "WATCHING")
    old_at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    new_at = old_at + timedelta(hours=3)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO airing_schedule_cache (anime_id, episode, airing_at) VALUES (%s, 5, %s)",
            (ANIME_A, new_at),
        )

    app_module._notify_airing_schedule_shifts({(ANIME_A, 5): old_at})

    assert len(sent) == 1
    user_id, title, body = sent[0]
    assert user_id == USER_ID
    assert "Frieren" in body
    assert "delayed" in body


def test_shift_below_threshold_does_not_notify(pg_conn, app_module, sent):
    _insert_anime(pg_conn, ANIME_A)
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "WATCHING")
    old_at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    new_at = old_at + timedelta(minutes=10)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO airing_schedule_cache (anime_id, episode, airing_at) VALUES (%s, 5, %s)",
            (ANIME_A, new_at),
        )

    app_module._notify_airing_schedule_shifts({(ANIME_A, 5): old_at})

    assert sent == []


def test_new_episode_row_not_in_before_snapshot_does_not_notify(pg_conn, app_module, sent):
    _insert_anime(pg_conn, ANIME_A)
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "WATCHING")
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO airing_schedule_cache (anime_id, episode, airing_at) VALUES (%s, 5, %s)",
            (ANIME_A, datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)),
        )

    app_module._notify_airing_schedule_shifts({})  # empty before — nothing to compare against

    assert sent == []


def test_shift_only_notifies_watching_or_planning_users(pg_conn, app_module, sent):
    _insert_anime(pg_conn, ANIME_A)
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "COMPLETED")
    _insert_library_entry(pg_conn, OTHER_USER_ID, ANIME_A, "PLANNING")
    old_at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    new_at = old_at + timedelta(hours=2)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO airing_schedule_cache (anime_id, episode, airing_at) VALUES (%s, 5, %s)",
            (ANIME_A, new_at),
        )

    app_module._notify_airing_schedule_shifts({(ANIME_A, 5): old_at})

    assert len(sent) == 1
    assert sent[0][0] == OTHER_USER_ID  # not the COMPLETED user


def test_shift_earlier_says_moved_earlier_not_delayed(pg_conn, app_module, sent):
    _insert_anime(pg_conn, ANIME_A)
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "WATCHING")
    old_at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    new_at = old_at - timedelta(hours=2)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO airing_schedule_cache (anime_id, episode, airing_at) VALUES (%s, 5, %s)",
            (ANIME_A, new_at),
        )

    app_module._notify_airing_schedule_shifts({(ANIME_A, 5): old_at})

    assert "moved earlier" in sent[0][2]


# ── #375 — monthly recap digest ─────────────────────────────────────────────


def test_compute_monthly_recap_returns_none_with_no_completions(pg_conn, app_module):
    recap = app_module._compute_monthly_recap(USER_ID, date(2026, 7, 1), date(2026, 7, 31))
    assert recap is None


def test_compute_monthly_recap_summarizes_completions_in_range(pg_conn, app_module):
    _insert_anime(pg_conn, ANIME_A, episodes=12, genres=["Action", "Drama"], title="Frieren")
    _insert_anime(pg_conn, ANIME_B, episodes=24, genres=["Action"], title="Bocchi")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "COMPLETED", date(2026, 7, 10))
    _insert_library_entry(pg_conn, USER_ID, ANIME_B, "COMPLETED", date(2026, 7, 20))
    # Outside the window — must not count.
    _insert_anime(pg_conn, 953, episodes=1, title="Outside")
    _insert_library_entry(pg_conn, USER_ID, 953, "COMPLETED", date(2026, 8, 1))

    recap = app_module._compute_monthly_recap(USER_ID, date(2026, 7, 1), date(2026, 7, 31))

    assert recap["completions"] == 2
    assert recap["total_episodes"] == 36
    assert recap["top_genre"] == "Action"  # appears in both, Drama only once
    assert set(recap["titles"]) == {"Frieren", "Bocchi"}


def test_scheduled_monthly_recap_notifies_users_with_completions_only(pg_conn, app_module, sent, monkeypatch):
    fixed_today = date(2026, 8, 1)

    class _FakeDate(date):
        @classmethod
        def today(cls):
            return fixed_today

    monkeypatch.setattr(app_module, "date", _FakeDate)

    _insert_anime(pg_conn, ANIME_A, episodes=12, genres=["Comedy"], title="Bocchi")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "COMPLETED", date(2026, 7, 15))
    # OTHER_USER_ID has nothing completed in July — must not get a digest.

    app_module._scheduled_monthly_recap()

    assert len(sent) == 1
    user_id, title, body = sent[0]
    assert user_id == USER_ID
    assert "July 2026" in title
    assert "Bocchi" in body
    assert "Comedy" in body
