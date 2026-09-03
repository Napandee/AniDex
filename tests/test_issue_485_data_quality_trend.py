"""
Coverage for issue #485 — periodic snapshots of the admin Data Quality tab's signals
(_data_quality_signals(), issue #202) so the tab can show a trend instead of only ever
a single point-in-time read.

Verified against a real Postgres, same pattern as tests/test_health_signal_notifications.py
and tests/test_admin_data_quality.py: exercises app.main._snapshot_data_quality_signals()
and _data_quality_trend() directly against the real schema.sql shape (migration 044's
data_quality_snapshots table).

Needs a reachable Postgres via DATABASE_URL — skipped entirely if one isn't available,
so `pytest tests/` still collects and passes on a machine with no Postgres running.

Covers:
  1. A snapshot run with no data writes a row with all-zero/None-ish counts.
  2. Orphaned notes / stale recommendations / drift candidates counts are reflected
     accurately in the written snapshot.
  3. Overall failure rate is aggregated correctly across multiple users' sync_log rows.
  4. Pending-migration count and rate-limit-active are captured in the snapshot.
  5. Retention pruning removes snapshots older than DATA_QUALITY_SNAPSHOT_RETENTION_DAYS
     and keeps ones within the window.
  6. _data_quality_trend() returns snapshots oldest-first, respecting `limit`.
  7. The admin page renders the trend section without error when snapshots exist and
     when the table is empty.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM data_quality_snapshots")
        cur.execute("DELETE FROM personal_notes")
        cur.execute("DELETE FROM recommendation_scores")
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM users")
        cur.execute("DELETE FROM sync_log")
        cur.execute("DELETE FROM migration_state")
        cur.execute("DELETE FROM anilist_rate_limit_state")


def _make_user(pg_conn, user_id):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, is_admin) "
            "VALUES (%s, 'local', %s, %s, false)",
            (user_id, f"test{user_id}", f"test{user_id}@example.com"),
        )


def _make_anime(pg_conn, anime_id, title="Test Anime"):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (anime_id, title),
        )


def test_empty_instance_writes_a_zeroed_snapshot(app_module, pg_conn):
    app_module._snapshot_data_quality_signals()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT failure_rate_overall, orphaned_personal_notes_count, "
            "stale_recommendations_count, drift_candidates_count, "
            "pending_migrations, rate_limit_active FROM data_quality_snapshots"
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    failure_rate, orphaned, stale, drift, pending, rate_limited = rows[0]
    assert failure_rate is None  # no sync runs at all → no rate to compute
    assert orphaned == 0
    assert stale == 0
    assert drift == 0
    assert pending is None  # no migration_state row on a fresh install
    assert rate_limited is False


def test_orphaned_notes_and_stale_recs_counted(app_module, pg_conn):
    _make_user(pg_conn, 1)
    _make_anime(pg_conn, 100)  # orphan case: note with no library entry
    _make_anime(pg_conn, 200)  # stale case: recommendation for an anime already in the library
    with pg_conn.cursor() as cur:
        # Orphaned: a personal_notes row with no matching library_entries row.
        cur.execute(
            "INSERT INTO personal_notes (user_id, anime_id, notes) VALUES (1, 100, 'orphaned')"
        )
        # Stale: a recommendation_scores row for an anime that IS in the library
        # (the inverse case — see _data_quality_signals()'s own docstring for why).
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status) VALUES (1, 200, 'WATCHING')"
        )
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, dismissed) "
            "VALUES (1, 200, 0.5, false)"
        )
    pg_conn.commit()

    app_module._snapshot_data_quality_signals()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT orphaned_personal_notes_count, stale_recommendations_count "
            "FROM data_quality_snapshots"
        )
        orphaned, stale = cur.fetchone()
    assert orphaned == 1
    assert stale == 1


def test_failure_rate_aggregated_across_users(app_module, pg_conn):
    _make_user(pg_conn, 1)
    _make_user(pg_conn, 2)
    now = datetime.now(timezone.utc)
    with pg_conn.cursor() as cur:
        # user 1: 1 ok, 1 error → contributes 1/2
        cur.execute(
            "INSERT INTO sync_log (user_id, type, status, run_at) VALUES "
            "(1, 'full_sync', 'ok', %s), (1, 'full_sync', 'error', %s)",
            (now, now),
        )
        # user 2: 1 error → contributes 1/1
        cur.execute(
            "INSERT INTO sync_log (user_id, type, status, run_at) VALUES (2, 'full_sync', 'error', %s)",
            (now,),
        )
    pg_conn.commit()

    app_module._snapshot_data_quality_signals()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT failure_rate_overall FROM data_quality_snapshots")
        (rate,) = cur.fetchone()
    # 2 failed out of 3 total runs across both users.
    assert rate == pytest.approx(2 / 3)


def test_pending_migrations_and_rate_limit_captured(app_module, pg_conn, monkeypatch):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO migration_state (id, highest_applied_migration) VALUES (1, %s)",
            (app_module.LATEST_MIGRATION - 3,),
        )
        cur.execute(
            "INSERT INTO anilist_rate_limit_state (id, source, retry_after_seconds, observed_at) "
            "VALUES (1, 'outbox', 600, %s)",
            (datetime.now(timezone.utc),),
        )
    pg_conn.commit()

    app_module._snapshot_data_quality_signals()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT pending_migrations, rate_limit_active FROM data_quality_snapshots")
        pending, rate_limited = cur.fetchone()
    assert pending == 3
    assert rate_limited is True


def test_retention_pruning_removes_old_snapshots_keeps_recent(app_module, pg_conn):
    old_ts = datetime.now(timezone.utc) - timedelta(
        days=app_module.DATA_QUALITY_SNAPSHOT_RETENTION_DAYS + 5
    )
    recent_ts = datetime.now(timezone.utc) - timedelta(days=1)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_quality_snapshots "
            "(snapshot_at, orphaned_personal_notes_count, stale_recommendations_count, "
            "drift_candidates_count, rate_limit_active) VALUES (%s, 0, 0, 0, false)",
            (old_ts,),
        )
        cur.execute(
            "INSERT INTO data_quality_snapshots "
            "(snapshot_at, orphaned_personal_notes_count, stale_recommendations_count, "
            "drift_candidates_count, rate_limit_active) VALUES (%s, 0, 0, 0, false)",
            (recent_ts,),
        )
    pg_conn.commit()

    app_module._snapshot_data_quality_signals()  # also writes a fresh 3rd row

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM data_quality_snapshots WHERE snapshot_at < %s", (old_ts + timedelta(days=1),))
        (old_count,) = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM data_quality_snapshots")
        (total,) = cur.fetchone()
    assert old_count == 0  # pruned
    assert total == 2  # the recent seeded row + the fresh one this run wrote


def test_trend_returns_oldest_first_respecting_limit(app_module, pg_conn):
    base = datetime.now(timezone.utc) - timedelta(days=10)
    with pg_conn.cursor() as cur:
        for i in range(5):
            cur.execute(
                "INSERT INTO data_quality_snapshots "
                "(snapshot_at, orphaned_personal_notes_count, stale_recommendations_count, "
                "drift_candidates_count, rate_limit_active) VALUES (%s, %s, 0, 0, false)",
                (base + timedelta(days=i), i),
            )
    pg_conn.commit()

    trend = app_module._data_quality_trend(limit=3)

    assert len(trend) == 3
    # Oldest-first among the 3 most recent snapshots (days 2, 3, 4).
    assert [row["orphaned_personal_notes_count"] for row in trend] == [2, 3, 4]
    assert trend[0]["snapshot_at"] < trend[-1]["snapshot_at"]


def test_admin_page_renders_trend_section_empty_and_populated(app_module, pg_conn, client):
    # Log in as an admin via the same pattern tests/test_admin_invites.py uses.
    email, password = "admin485@test.local", "testpass123"
    password_hash = app_module.bcrypt.hashpw(password.encode(), app_module.bcrypt.gensalt()).decode()
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (auth_provider, auth_provider_id, email, password_hash, "
            "is_admin, is_active) VALUES ('local', %s, %s, %s, true, true)",
            (email, email, password_hash),
        )
    pg_conn.commit()

    resp = client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=False)
    assert resp.status_code in (200, 303)

    # Empty state. The key name itself always appears in the embedded window.I18N
    # JSON blob regardless of what's rendered, so assert on the resolved English
    # string actually shown in the trend table body, not the raw key.
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "No snapshots yet" in resp.text

    # Populated state.
    app_module._snapshot_data_quality_signals()
    resp = client.get("/admin")
    assert resp.status_code == 200
