"""
Regression coverage for sync_anilist.py's upsert_library_entry() WHERE-guard
(issue #46) — the query that writes the app's core personal-library data, so this is
deliberately verified against a real Postgres rather than a mocked cursor: a mock
would only prove the Python calls cur.execute(), not that the SQL's IS DISTINCT FROM
guard actually distinguishes "changed" from "unchanged" the way rowcount-based
counting in scripts/run_full_sync.py (and now sync_anilist.py's own SYNC_RESULT line)
depends on. Same "don't trust a green run, verify actual rowcount behavior" lesson as
the 2026-08-13 fastapi/starlette outage — see CLAUDE.local.md.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml already uses for the app smoke test) — skipped
entirely if one isn't available, so `pytest tests/` still collects and passes on a
machine with no Postgres running. Applies schema.sql fresh against a clean `public`
schema at module setup so this always runs against the real target shape rather than
a hand-duplicated subset of it that could drift from schema.sql over time.
"""

import os
from pathlib import Path

import psycopg2
import pytest

import sync_anilist as sa

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
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
            "VALUES (1, 'local', 'test', 'test@example.com')"
        )
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (555, 'Test Anime')"
        )
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_library_entries(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM library_entries")


def _entry(**overrides) -> dict:
    base = {
        "id": 999,
        "status": "CURRENT",
        "score": 4.0,
        "progress": 5,
        "repeat": 0,
        "startedAt": None,
        "completedAt": None,
        "updatedAt": 1700000000,
        "media": {"id": 555},
    }
    base.update(overrides)
    return base


def test_fresh_insert_counts_as_touched(pg_conn):
    with pg_conn.cursor() as cur:
        touched = sa.upsert_library_entry(cur, _entry())
    assert touched is True

    with pg_conn.cursor() as cur:
        cur.execute("SELECT status, progress, score FROM library_entries WHERE anime_id = 555")
        row = cur.fetchone()
    assert row == ("WATCHING", 5, 4.0)


def test_rerunning_identical_data_touches_nothing(pg_conn):
    with pg_conn.cursor() as cur:
        sa.upsert_library_entry(cur, _entry())

    with pg_conn.cursor() as cur:
        touched = sa.upsert_library_entry(cur, _entry())
    assert touched is False


def test_changing_one_field_touches_exactly_that_row(pg_conn):
    with pg_conn.cursor() as cur:
        sa.upsert_library_entry(cur, _entry())

    with pg_conn.cursor() as cur:
        touched = sa.upsert_library_entry(cur, _entry(progress=6))
    assert touched is True

    with pg_conn.cursor() as cur:
        cur.execute("SELECT progress FROM library_entries WHERE anime_id = 555")
        assert cur.fetchone() == (6,)


def test_pending_outbox_row_is_never_touched(pg_conn):
    """Issue #18's sync_status = 'pending' guard must still win even under the new
    IS DISTINCT FROM condition — a pending row must never be overwritten by an
    in-flight pull sync, regardless of whether the incoming data actually differs."""
    with pg_conn.cursor() as cur:
        sa.upsert_library_entry(cur, _entry())
        cur.execute("UPDATE library_entries SET sync_status = 'pending' WHERE anime_id = 555")

    with pg_conn.cursor() as cur:
        touched = sa.upsert_library_entry(cur, _entry(progress=99))
    assert touched is False

    with pg_conn.cursor() as cur:
        cur.execute("SELECT progress FROM library_entries WHERE anime_id = 555")
        assert cur.fetchone() == (5,)


def test_null_score_never_clobbers_existing_score(pg_conn):
    """score uses COALESCE(EXCLUDED.score, library_entries.score) in the SET clause —
    the WHERE guard's own score comparison must match that semantics (only compare
    when EXCLUDED.score is not null) or a null-score AniList payload would either
    wrongly report "touched" every run, or wrongly suppress a real change elsewhere."""
    with pg_conn.cursor() as cur:
        sa.upsert_library_entry(cur, _entry(score=4.0))

    with pg_conn.cursor() as cur:
        touched = sa.upsert_library_entry(cur, _entry(score=None))
    assert touched is False

    with pg_conn.cursor() as cur:
        cur.execute("SELECT score FROM library_entries WHERE anime_id = 555")
        assert cur.fetchone() == (4.0,)
