"""
Regression coverage for scripts/sync_id_mappings.py (issue #447) — the
AniDB/MAL -> AniList id mapping cache sourced from Fribb/anime-lists, feeding
sync_plex.py's Guid-based fast path.

The upstream fetch is always mocked at the httpx.get level, matching this
repo's established pattern for external-API-backed sync scripts (see
tests/test_sync_manga_data.py). The real anime-list-mini.json shape (list of
dicts with integer anilist_id/anidb_id/mal_id fields, some entries missing
one or more of them) was confirmed via a real live fetch while building this
— never in CI.

DB-touching tests run against a real throwaway Postgres (same pg_conn fixture
pattern as test_sync_manga_data.py) rather than a mocked cursor.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

import sync_id_mappings as sim

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _try_connect():
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=2, cursor_factory=psycopg2.extras.RealDictCursor)
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
def _clean_table(pg_conn):
    """NOT autouse — see test_sync_manga_data.py's identical rationale: the
    pure-function tests below need no DB at all."""
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM anidb_mal_mapping_cache")


# ── Pure-function unit coverage (no DB, no network) ──────────────────────────

def test_parse_mapping_rows_keeps_rows_with_anilist_and_at_least_one_other_id():
    rows = [
        {"anilist_id": 290, "anidb_id": 1, "mal_id": 290},
        {"anilist_id": 300, "mal_id": 300},
        {"anilist_id": 4, "anidb_id": 4},
    ]
    parsed = sim.parse_mapping_rows(rows)
    assert parsed == [(290, 1, 290), (300, None, 300), (4, 4, None)]


def test_parse_mapping_rows_skips_rows_missing_anilist_id():
    rows = [{"anidb_id": 1, "mal_id": 290}, {"anilist_id": None, "mal_id": 5}]
    assert sim.parse_mapping_rows(rows) == []


def test_parse_mapping_rows_skips_rows_with_neither_anidb_nor_mal_id():
    rows = [{"anilist_id": 290, "tvdb_id": 72025}]
    assert sim.parse_mapping_rows(rows) == []


def test_parse_mapping_rows_tolerates_non_int_ids():
    rows = [{"anilist_id": 290, "anidb_id": "not-an-int", "mal_id": 290}]
    assert sim.parse_mapping_rows(rows) == [(290, None, 290)]


def test_fetch_mapping_rows_raises_on_non_list_payload(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"not": "a list"}

    monkeypatch.setattr(sim.httpx, "get", lambda *a, **k: _Resp())
    with pytest.raises(ValueError):
        sim.fetch_mapping_rows()


# ── DB-backed coverage ────────────────────────────────────────────────────────

def test_replace_mapping_cache_inserts_rows(pg_conn, _clean_table):
    sim.replace_mapping_cache(pg_conn, [(290, 1, 290), (300, None, 300)], NOW)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT * FROM anidb_mal_mapping_cache ORDER BY anilist_id")
        rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0]["anilist_id"] == 290
    assert rows[0]["anidb_id"] == 1
    assert rows[0]["mal_id"] == 290
    assert rows[1]["anidb_id"] is None


def test_replace_mapping_cache_fully_replaces_stale_rows(pg_conn, _clean_table):
    sim.replace_mapping_cache(pg_conn, [(290, 1, 290)], NOW)
    sim.replace_mapping_cache(pg_conn, [(300, None, 300)], NOW)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT anilist_id FROM anidb_mal_mapping_cache")
        rows = cur.fetchall()
    assert [r["anilist_id"] for r in rows] == [300]
