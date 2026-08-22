"""
Regression coverage for scripts/sync_filler_data.py (issue #299) — the AniFillerPedia
filler/canon episode cache that #300/#301/#302 build their UI on top of.

The external AniFillerPedia API is always mocked at the httpx.get level, matching
this repo's established pattern for external-API-backed sync scripts (see
tests/test_credential_check.py, tests/test_planning_coverage_notify_wiring.py) —
never a real network call in CI, even though a real one was used as a one-off manual
sanity check while building this (https://anifillerpedia.wiki/api/v1, confirmed the
documented response shapes for /series, /series/{id}/episodes, and /license).

DB-touching tests run against a real throwaway Postgres (same pg_conn fixture
pattern as test_sync_anilist_upsert.py) rather than a mocked cursor, so the actual
upsert/delete SQL is verified against the real schema.sql shape, not a
hand-duplicated subset of it that could drift. Skipped entirely if no Postgres is
reachable, so `pytest tests/` still collects and passes on a machine with none
running.

The "already checked recently, don't re-query every run" logic (is_due/
compute_due_anime_ids) is pure and DB-free, so it's covered separately below with
plain unit tests.
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

import sync_filler_data as sfd

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
        # subset of it — so this can't silently drift from the actual table shape.
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(SCHEMA_SQL)
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (555, 'Test Anime A'), (556, 'Test Anime B')"
        )
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_filler_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM filler_episode_cache")
        cur.execute("DELETE FROM filler_sync_state")
        cur.execute("DELETE FROM filler_data_license")


NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _install_fake_get(monkeypatch, *, series_by_anilist_id=None, episodes_by_series_id=None, license_payload=None):
    """series_by_anilist_id: {anilist_id: [items...]} — an empty/missing list means no match.
    episodes_by_series_id: {series_id: [episode...]}."""
    series_by_anilist_id = series_by_anilist_id or {}
    episodes_by_series_id = episodes_by_series_id or {}

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/license"):
            return _FakeResponse(license_payload or {})
        if url.endswith("/series"):
            anilist_id = params["anilist_id"]
            items = series_by_anilist_id.get(anilist_id, [])
            return _FakeResponse({"items": items, "total": len(items)})
        if "/series/" in url and url.endswith("/episodes"):
            series_id = int(url.rsplit("/", 2)[1])
            return _FakeResponse(episodes_by_series_id.get(series_id, []))
        raise AssertionError(f"unexpected URL in test: {url}")

    monkeypatch.setattr(sfd.httpx, "get", fake_get)


# ── Matched series with real episode data — upserts into the cache table ────────

def test_matched_series_with_episodes_upserts_cache_rows(pg_conn, monkeypatch):
    _install_fake_get(
        monkeypatch,
        series_by_anilist_id={555: [{"id": 42, "anilist_id": 555, "title": "Test Anime A"}]},
        episodes_by_series_id={
            42: [
                {
                    "id": 1, "series_id": 42, "episode_number": 1, "status": "canon",
                    "status_note": None, "citation": {"id": 9, "url": None, "description": "Source A"},
                    "updated_at": "2026-08-01T00:00:00Z",
                },
                {
                    "id": 2, "series_id": 42, "episode_number": 2, "status": "filler",
                    "status_note": "Not in the manga", "citation": {"id": 9, "url": "https://x.example", "description": "Source A"},
                    "updated_at": "2026-08-01T00:00:00Z",
                },
            ]
        },
    )

    sfd.sync_one_anime(pg_conn, 555, NOW)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT episode_number, status, status_note, citation_url, citation_description "
            "FROM filler_episode_cache WHERE anime_id = 555 ORDER BY episode_number"
        )
        rows = cur.fetchall()
    assert rows == [
        (1, "canon", None, None, "Source A"),
        (2, "filler", "Not in the manga", "https://x.example", "Source A"),
    ]

    with pg_conn.cursor() as cur:
        cur.execute("SELECT afp_series_id, last_checked_at FROM filler_sync_state WHERE anime_id = 555")
        state = cur.fetchone()
    assert state == (42, NOW)


def test_resyncing_replaces_stale_episode_rows(pg_conn, monkeypatch):
    """A corrected/retracted episode on AniFillerPedia's side must be reflected, not
    left stale from a previous run — sync_one_anime deletes+reinserts per anime_id."""
    _install_fake_get(
        monkeypatch,
        series_by_anilist_id={555: [{"id": 42, "anilist_id": 555}]},
        episodes_by_series_id={
            42: [{"id": 1, "series_id": 42, "episode_number": 1, "status": "canon",
                  "status_note": None, "citation": {"id": 1, "url": None, "description": "d"},
                  "updated_at": "x"}]
        },
    )
    sfd.sync_one_anime(pg_conn, 555, NOW)

    later = NOW + timedelta(days=1)
    _install_fake_get(
        monkeypatch,
        series_by_anilist_id={555: [{"id": 42, "anilist_id": 555}]},
        episodes_by_series_id={
            42: [{"id": 1, "series_id": 42, "episode_number": 1, "status": "mixed",
                  "status_note": "corrected", "citation": {"id": 1, "url": None, "description": "d"},
                  "updated_at": "y"}]
        },
    )
    sfd.sync_one_anime(pg_conn, 555, later)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT status, status_note FROM filler_episode_cache WHERE anime_id = 555")
        rows = cur.fetchall()
    assert rows == [("mixed", "corrected")]


# ── Matched series, zero researched episodes — no error, nothing cached ─────────

def test_matched_series_with_zero_episodes_caches_nothing(pg_conn, monkeypatch):
    _install_fake_get(
        monkeypatch,
        series_by_anilist_id={556: [{"id": 43, "anilist_id": 556}]},
        episodes_by_series_id={43: []},
    )

    sfd.sync_one_anime(pg_conn, 556, NOW)  # must not raise

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM filler_episode_cache WHERE anime_id = 556")
        assert cur.fetchone() == (0,)
        cur.execute("SELECT afp_series_id, last_checked_at FROM filler_sync_state WHERE anime_id = 556")
        assert cur.fetchone() == (43, NOW)


# ── No series match at all — no error, nothing cached ───────────────────────────

def test_no_series_match_caches_nothing(pg_conn, monkeypatch):
    _install_fake_get(monkeypatch, series_by_anilist_id={555: []})

    sfd.sync_one_anime(pg_conn, 555, NOW)  # must not raise

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM filler_episode_cache WHERE anime_id = 555")
        assert cur.fetchone() == (0,)
        cur.execute("SELECT afp_series_id, last_checked_at FROM filler_sync_state WHERE anime_id = 555")
        assert cur.fetchone() == (None, NOW)


# ── License/attribution caching (acceptance criterion #4) ───────────────────────

def test_sync_license_upserts_singleton_row(pg_conn, monkeypatch):
    _install_fake_get(
        monkeypatch,
        license_payload={
            "license": "CC BY-NC-SA 4.0",
            "attribution_notice": "Contains information from AniFillerPedia...",
            "dataset_license_url": "https://example.invalid/DATA_LICENSE",
        },
    )

    sfd.sync_license(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT license_name, attribution_notice, raw_response FROM filler_data_license WHERE id = 1")
        row = cur.fetchone()
    assert row[0] == "CC BY-NC-SA 4.0"
    assert row[1] == "Contains information from AniFillerPedia..."
    assert row[2]["dataset_license_url"] == "https://example.invalid/DATA_LICENSE"

    # Re-running overwrites the same singleton row rather than erroring or duplicating.
    sfd.sync_license(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM filler_data_license")
        assert cur.fetchone() == (1,)


# ── "Already checked recently" skip logic (acceptance criterion #3) ─────────────

def test_never_checked_title_is_always_due():
    assert sfd.is_due(afp_series_id=None, last_checked_at=None, now=NOW) is True
    assert sfd.is_due(afp_series_id=42, last_checked_at=None, now=NOW) is True


def test_matched_title_checked_recently_is_not_due():
    checked = NOW - timedelta(days=1)
    assert sfd.is_due(afp_series_id=42, last_checked_at=checked, now=NOW) is False


def test_matched_title_past_its_recheck_interval_is_due():
    checked = NOW - sfd.RECHECK_INTERVAL_MATCHED - timedelta(days=1)
    assert sfd.is_due(afp_series_id=42, last_checked_at=checked, now=NOW) is True


def test_unmatched_title_gets_a_longer_cooldown_than_a_matched_one():
    """Core of issue #299's own open question: an unmatched title shouldn't be
    re-queried as often as a matched-but-thin one."""
    checked = NOW - sfd.RECHECK_INTERVAL_MATCHED - timedelta(days=1)
    # Past the *matched* interval, but not the (longer) no-match interval.
    assert sfd.is_due(afp_series_id=42, last_checked_at=checked, now=NOW) is True
    assert sfd.is_due(afp_series_id=None, last_checked_at=checked, now=NOW) is False


def test_unmatched_title_past_its_own_longer_interval_is_due():
    checked = NOW - sfd.RECHECK_INTERVAL_NO_MATCH - timedelta(days=1)
    assert sfd.is_due(afp_series_id=None, last_checked_at=checked, now=NOW) is True


def test_compute_due_anime_ids_filters_the_full_catalog():
    state = {
        555: (42, NOW - timedelta(days=1)),               # matched, checked yesterday -> not due
        556: (None, NOW - sfd.RECHECK_INTERVAL_NO_MATCH - timedelta(days=1)),  # unmatched, stale -> due
        # 557 never checked -> due
    }
    due = sfd.compute_due_anime_ids([555, 556, 557], state, NOW)
    assert sorted(due) == [556, 557]
