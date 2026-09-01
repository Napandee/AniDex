"""
Regression coverage for scripts/sync_manga_data.py (issue #454, direction decided
in spike #450) — the manga/light-novel "living integration" cache built on a
three-source pipeline: AniList's own `relations` field, MangaDex (as a bridge,
validated against a real AniList id — never a similarity score), and
MangaUpdates (real chapter/status/licensor data).

External APIs are always mocked at the httpx.get/post level, matching this
repo's established pattern for external-API-backed sync scripts (see
tests/test_sync_filler_data.py). Real, live network calls against AniList,
MangaDex, and MangaUpdates were used as one-off manual sanity checks while
building this (confirmed the documented response shapes, including the
`links.al`/`links.mu` cross-reference fields MangaDex actually returns) — never
in CI.

DB-touching tests run against a real throwaway Postgres (same pg_conn fixture
pattern as test_sync_filler_data.py) rather than a mocked cursor, so the actual
upsert SQL is verified against the real schema.sql shape.
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

import sync_manga_data as smd

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _try_connect():
    try:
        # RealDictCursor so this test file's own verification queries can index
        # rows by column name — sync_manga_data.py's own functions work with
        # whatever cursor_factory the conn they're passed already has, so this
        # doesn't change what's actually exercised in production (db_connect()
        # there sets the same RealDictCursor factory).
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
def _clean_tables(pg_conn):
    """NOT autouse — an autouse fixture that itself depends on pg_conn would
    force pg_conn to resolve (and skip, absent a reachable Postgres) for every
    test in this module, including the pure-function tests below that need no
    DB at all. Every DB-touching test below requests this explicitly."""
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM manga_adaptation_cache")
        cur.execute("DELETE FROM manga_adaptation_sync_state")
        cur.execute("DELETE FROM anime")


def _insert_anime(conn, anime_id, title=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (%s, %s)",
            (anime_id, title or f"Anime {anime_id}"),
        )


# ── Pure-function unit coverage (no DB, no network) ──────────────────────────

def test_is_due_never_checked_is_always_due():
    assert smd.is_due(None, None, NOW) is True


def test_is_due_matched_uses_shorter_cooldown():
    recent = NOW - timedelta(days=2)
    assert smd.is_due(True, recent, NOW) is False
    stale = NOW - timedelta(days=7)
    assert smd.is_due(True, stale, NOW) is True


def test_is_due_no_match_uses_longer_cooldown():
    recent = NOW - timedelta(days=10)
    assert smd.is_due(False, recent, NOW) is False
    stale = NOW - timedelta(days=31)
    assert smd.is_due(False, stale, NOW) is True


def test_compute_due_anime_ids_filters_the_full_catalog():
    state = {
        1: (True, NOW - timedelta(days=1)),   # matched, recently checked — not due
        2: (True, NOW - timedelta(days=10)),  # matched, stale — due
        3: (False, NOW - timedelta(days=1)),  # no-match, recently checked — not due
    }
    due = smd.compute_due_anime_ids([1, 2, 3, 4], state, NOW)
    assert due == [2, 4]  # 4 was never checked — always due


def test_english_licensor_from_links_prefers_streaming_over_info():
    links = [
        {"site": "Official Site", "type": "INFO", "language": "English"},
        {"site": "VIZ", "type": "STREAMING", "language": "English", "url": "https://viz.example"},
        {"site": "Comico", "type": "STREAMING", "language": "Japanese"},
    ]
    name, url = smd.english_licensor_from_links(links)
    assert name == "VIZ"
    assert url == "https://viz.example"


def test_english_licensor_from_links_none_when_no_english_link():
    links = [{"site": "Comico", "type": "STREAMING", "language": "Japanese"}]
    assert smd.english_licensor_from_links(links) == (None, None)


def test_english_publisher_from_mangaupdates_picks_english_type():
    series = {"publishers": [
        {"publisher_name": "Shogakukan", "type": "Original"},
        {"publisher_name": "Viz", "type": "English"},
    ]}
    assert smd.english_publisher_from_mangaupdates(series) == "Viz"


def test_english_publisher_from_mangaupdates_none_when_no_english_publisher():
    series = {"publishers": [{"publisher_name": "Shogakukan", "type": "Original"}]}
    assert smd.english_publisher_from_mangaupdates(series) is None


# ── Mocked pipeline coverage ──────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload

    @property
    def is_success(self):
        return 200 <= self.status_code < 300


def test_fetch_source_candidates_filters_to_source_manga_edges(monkeypatch):
    """Only relationType=SOURCE + node.type=MANGA edges count — a SEQUEL/PREQUEL
    edge, or a SOURCE edge pointing at a non-manga type (e.g. a visual novel,
    which AniList's `type` enum still reports as something other than MANGA at
    this level), must both be ignored."""
    def fake_post(url, json=None, headers=None, timeout=None):
        assert json["variables"] == {"id": 1}
        return _FakeResponse({"data": {"Media": {"relations": {"edges": [
            {"relationType": "SEQUEL", "node": {"id": 2, "type": "ANIME", "title": {"romaji": "Sequel"}}},
            {"relationType": "SOURCE", "node": {"id": 3, "type": "MANGA", "title": {"romaji": "The Manga", "english": "The Manga EN"}}},
            {"relationType": "SOURCE", "node": {"id": 4, "type": "GAME", "title": {"romaji": "Visual Novel Source"}}},
        ]}}}})

    monkeypatch.setattr(smd.httpx, "post", fake_post)
    candidates = smd.fetch_source_candidates(1)
    assert candidates == [{"id": 3, "title": "The Manga EN"}]


def test_resolve_mangadex_match_requires_exact_al_equality(monkeypatch):
    """A MangaDex hit whose links.al doesn't match the AniList id we already
    have must be rejected outright — no partial credit, no similarity score."""
    def fake_get(url, params=None, timeout=None):
        assert "/manga" in url
        return _FakeResponse({"data": [
            {"id": "wrong-hit", "attributes": {"links": {"al": "999"}}},
            {"id": "right-hit", "attributes": {"links": {"al": "118586", "mu": "abc123"}}},
        ]})

    monkeypatch.setattr(smd.httpx, "get", fake_get)
    hit = smd.resolve_mangadex_match("Some Manga", 118586)
    assert hit == {"id": "right-hit", "mu_slug": "abc123"}


def test_resolve_mangadex_match_none_when_no_hit_matches(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return _FakeResponse({"data": [{"id": "wrong-hit", "attributes": {"links": {"al": "999"}}}]})

    monkeypatch.setattr(smd.httpx, "get", fake_get)
    assert smd.resolve_mangadex_match("Some Manga", 118586) is None


def test_resolve_mangaupdates_series_id_requires_slug_in_url(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        assert "/series/search" in url
        return _FakeResponse({"results": [
            {"record": {"series_id": 111, "url": "https://www.mangaupdates.com/series/other-slug/x"}},
            {"record": {"series_id": 222, "url": "https://www.mangaupdates.com/series/abc123/the-manga"}},
        ]})

    monkeypatch.setattr(smd.httpx, "post", fake_post)
    assert smd.resolve_mangaupdates_series_id("The Manga", "abc123") == 222


def test_resolve_mangaupdates_series_id_none_without_slug():
    assert smd.resolve_mangaupdates_series_id("The Manga", None) is None


def _install_full_pipeline_mocks(monkeypatch, *, format="MANGA", mangadex_hit=True, mangaupdates_hit=True):
    def fake_post(url, json=None, headers=None, timeout=None):
        if "graphql.anilist.co" in url:
            return _FakeResponse({"data": {"Media": {
                "id": 118586, "format": format, "status": "RELEASING",
                "chapters": None, "volumes": None,
                "externalLinks": [{"site": "VIZ", "type": "STREAMING", "language": "English", "url": "https://viz.example"}],
            }}})
        if "/series/search" in url:
            if not mangaupdates_hit:
                return _FakeResponse({"results": []})
            return _FakeResponse({"results": [
                {"record": {"series_id": 66296374554, "url": "https://www.mangaupdates.com/series/abc123/x"}},
            ]})
        raise AssertionError(f"unexpected POST url: {url}")

    def fake_get(url, params=None, timeout=None):
        if "/manga" in url and "mangadex" in url:
            if not mangadex_hit:
                return _FakeResponse({"data": []})
            return _FakeResponse({"data": [
                {"id": "mangadex-id-1", "attributes": {"links": {"al": "118586", "mu": "abc123"}}},
            ]})
        if "/series/" in url:
            return _FakeResponse({
                "latest_chapter": 147,
                "last_updated": {"as_rfc3339": "2026-08-28T00:33:58-07:00"},
                "publishers": [{"publisher_name": "Viz", "type": "English"}],
            })
        raise AssertionError(f"unexpected GET url: {url}")

    monkeypatch.setattr(smd.httpx, "post", fake_post)
    monkeypatch.setattr(smd.httpx, "get", fake_get)
    monkeypatch.setattr(smd.time, "sleep", lambda *_: None)


def test_resolve_one_source_full_pipeline_success(monkeypatch):
    _install_full_pipeline_mocks(monkeypatch)
    source_type, row = smd.resolve_one_source(118586, "Frieren: Beyond Journey's End")
    assert source_type == "MANGA"
    assert row["match_method"] == "mangadex_verified"
    assert row["latest_chapter"] == 147
    assert row["licensor_name"] == "Viz"
    assert row["mangadex_id"] == "mangadex-id-1"
    assert row["mangaupdates_id"] == "66296374554"


def test_resolve_one_source_novel_format_maps_to_novel_type(monkeypatch):
    _install_full_pipeline_mocks(monkeypatch, format="NOVEL")
    source_type, row = smd.resolve_one_source(118586, "Some Light Novel")
    assert source_type == "NOVEL"


def test_resolve_one_source_one_shot_maps_to_manga_type(monkeypatch):
    _install_full_pipeline_mocks(monkeypatch, format="ONE_SHOT")
    source_type, row = smd.resolve_one_source(118586, "Some One-Shot")
    assert source_type == "MANGA"


def test_resolve_one_source_untracked_format_returns_none(monkeypatch):
    _install_full_pipeline_mocks(monkeypatch, format="GAME")
    assert smd.resolve_one_source(118586, "Some Visual Novel") is None


def test_resolve_one_source_falls_back_to_anilist_only_when_mangadex_misses(monkeypatch):
    _install_full_pipeline_mocks(monkeypatch, mangadex_hit=False)
    source_type, row = smd.resolve_one_source(118586, "Frieren: Beyond Journey's End")
    assert row["match_method"] == "anilist_only"
    assert row["latest_chapter"] is None  # AniList's own chapters field was null (ongoing)
    assert row["licensor_name"] == "VIZ"  # still real, from AniList's own externalLinks
    assert row["mangadex_id"] is None


def test_resolve_one_source_falls_back_to_anilist_only_when_mangaupdates_misses(monkeypatch):
    _install_full_pipeline_mocks(monkeypatch, mangaupdates_hit=False)
    source_type, row = smd.resolve_one_source(118586, "Frieren: Beyond Journey's End")
    assert row["match_method"] == "anilist_only"
    assert row["mangadex_id"] == "mangadex-id-1"  # MangaDex step still succeeded
    assert row["mangaupdates_id"] is None


# ── DB-backed coverage: save_adaptation / save_sync_state / sync_one_anime ──

def test_sync_one_anime_upserts_adaptation_and_sync_state(pg_conn, _clean_tables, monkeypatch):
    _insert_anime(pg_conn, 154587)
    monkeypatch.setattr(smd, "fetch_source_candidates", lambda anime_id: [{"id": 118586, "title": "Frieren"}])
    _install_full_pipeline_mocks(monkeypatch)

    found = smd.sync_one_anime(pg_conn, 154587, NOW)
    assert found is True

    with pg_conn.cursor() as cur:
        cur.execute("SELECT source_type, latest_chapter, match_method FROM manga_adaptation_cache WHERE anime_id = 154587")
        row = cur.fetchone()
        assert row["source_type"] == "MANGA"
        assert row["latest_chapter"] == 147
        assert row["match_method"] == "mangadex_verified"

        cur.execute("SELECT has_adaptation FROM manga_adaptation_sync_state WHERE anime_id = 154587")
        state_row = cur.fetchone()
        assert state_row["has_adaptation"] is True


def test_sync_one_anime_records_no_match_state_when_no_candidates(pg_conn, _clean_tables, monkeypatch):
    _insert_anime(pg_conn, 1)
    monkeypatch.setattr(smd, "fetch_source_candidates", lambda anime_id: [])

    found = smd.sync_one_anime(pg_conn, 1, NOW)
    assert found is False

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM manga_adaptation_cache WHERE anime_id = 1")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT has_adaptation FROM manga_adaptation_sync_state WHERE anime_id = 1")
        assert cur.fetchone()["has_adaptation"] is False


def test_save_adaptation_upsert_overwrites_on_rerun(pg_conn, _clean_tables):
    _insert_anime(pg_conn, 42)
    row = {
        "anilist_source_id": 100, "title": "V1", "status": "RELEASING",
        "latest_chapter": 5, "latest_volume": None, "last_release_at": None,
        "licensor_name": "VIZ", "licensor_url": None, "cover_image_url": None,
        "mangadex_id": None, "mangaupdates_id": None, "match_method": "anilist_only",
    }
    smd.save_adaptation(pg_conn, 42, "MANGA", row, NOW)
    row["latest_chapter"] = 10
    row["title"] = "V2"
    smd.save_adaptation(pg_conn, 42, "MANGA", row, NOW)
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n, max(latest_chapter) AS ch FROM manga_adaptation_cache WHERE anime_id = 42")
        result = cur.fetchone()
        assert result["n"] == 1  # UNIQUE (anime_id, source_type) — upsert, not a second row
        assert result["ch"] == 10
