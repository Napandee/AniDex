"""
Coverage for _instance_health() (issue #86) — the read-only admin-panel query that
reports the running build version, Postgres database size, and row counts for
library_entries/anime/users. Verified against a real Postgres rather than a mocked
cursor, same pattern as tests/test_sync_anilist_upsert.py: this exercises real
aggregate SQL (pg_database_size, COUNT(*)) against the actual schema.sql shape, not
a hand-duplicated subset of it that could drift.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no
Postgres running.
"""

import os
import sys
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
        # Fully clean slate each run, applying the real schema.sql — not a hand-kept
        # subset of it — so this test can't silently drift from the actual table shape.
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(SCHEMA_SQL)
    yield conn
    conn.close()


@pytest.fixture()
def instance_health(pg_conn, monkeypatch):
    """Import app.main lazily (after DATABASE_URL/SESSION_SECRET_KEY are set by
    conftest.py) and return its _instance_health function."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    return m._instance_health


def test_row_counts_reflect_real_table_contents(pg_conn, instance_health):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
            "VALUES (1, 'local', 'test', 'test@example.com')"
        )
        cur.execute("INSERT INTO anime (id, title_romaji) VALUES (555, 'Test Anime')")
        cur.execute("INSERT INTO anime (id, title_romaji) VALUES (556, 'Test Anime 2')")
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress) "
            "VALUES (1, 555, 'WATCHING', 3)"
        )

    result = instance_health()

    assert result["row_counts"] == {
        "library_entries": 1,
        "anime": 2,
        "users": 1,
    }
    assert result["db_size"]  # some pg_size_pretty string, e.g. "8553 kB"


def test_build_version_unknown_when_git_sha_unset(instance_health, monkeypatch):
    monkeypatch.delenv("GIT_SHA", raising=False)
    assert instance_health()["build_version"] is None


def test_build_version_present_and_truncated_when_git_sha_set(instance_health, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abcdef0123456789")
    assert instance_health()["build_version"] == "abcdef012345"


# ── Pending migrations (issue #380) ─────────────────────────────────────────


def test_no_migration_state_row_reports_unknown_not_zero(pg_conn, instance_health):
    """schema.sql (a fresh install) creates migration_state with no seed row —
    see that table's own comment for why there's deliberately no INSERT in
    schema.sql. Unknown (None), not 0 — 0 would incorrectly claim confidence
    this function doesn't have."""
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM migration_state")
    assert instance_health()["pending_migrations"] is None


def test_marker_at_latest_reports_zero_pending(pg_conn, instance_health):
    import app.main as m

    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO migration_state (id, highest_applied_migration) VALUES (1, %s) "
            "ON CONFLICT (id) DO UPDATE SET highest_applied_migration = EXCLUDED.highest_applied_migration",
            (m.LATEST_MIGRATION,),
        )
    assert instance_health()["pending_migrations"] == 0


def test_marker_behind_latest_reports_real_gap(pg_conn, instance_health):
    """The exact scenario issue #380 exists to catch: code expects migration N,
    the database has only confirmed up to some earlier number."""
    import app.main as m

    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO migration_state (id, highest_applied_migration) VALUES (1, %s) "
            "ON CONFLICT (id) DO UPDATE SET highest_applied_migration = EXCLUDED.highest_applied_migration",
            (m.LATEST_MIGRATION - 3,),
        )
    assert instance_health()["pending_migrations"] == 3


def test_marker_ahead_of_latest_never_reports_negative(pg_conn, instance_health):
    """Shouldn't happen in practice (the marker can't legitimately exceed what
    the running code even knows about), but a negative "pending" count would be
    a confusing display bug if it ever did — clamp to 0."""
    import app.main as m

    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO migration_state (id, highest_applied_migration) VALUES (1, %s) "
            "ON CONFLICT (id) DO UPDATE SET highest_applied_migration = EXCLUDED.highest_applied_migration",
            (m.LATEST_MIGRATION + 5,),
        )
    assert instance_health()["pending_migrations"] == 0


def test_missing_migration_state_table_reports_unknown_not_a_crash(pg_conn, instance_health):
    """An instance mid-upgrade that hasn't reached migration 035 itself yet
    won't have this table at all — the exact kind of gap #380 exists to catch,
    ironically self-referential here. Must degrade gracefully, not break the
    whole Instance Health page."""
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE migration_state")
    try:
        assert instance_health()["pending_migrations"] is None
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE migration_state (
                    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                    highest_applied_migration INTEGER NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
