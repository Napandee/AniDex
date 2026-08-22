"""
Coverage for issue #284 — Streaming cancel-candidate view: the inverse of #255's
subscribe-focused set-cover recommendation. For each service a user already owns
(`user_streaming_services`, added for #182 — no new migration needed, see #284's
"open questions" note in schema.sql/main.py), what fraction of their actual
Watching/Planning list would become uncovered if that service were cancelled.

Same throwaway-Postgres pattern as tests/test_streaming_setcover.py — skipped
entirely if one isn't reachable.

Covers the acceptance-criteria scenarios from issue #284:
  1. A user can already mark owned services via `user_streaming_services` (reused,
     not reinvented) — verified indirectly by seeding that table directly, same as
     every other streaming test in this repo.
  2. Per-owned-service cancel impact: a service whose entire coverage overlaps
     another owned service reports zero titles/0% and is flagged fully_redundant;
     a service with unique coverage reports the correct count/percentage and title
     list.
  3. The denominator is the actual Watching/Planning list count (including titles
     with no recognized streaming link at all), not the broader Upcoming-inclusive
     set-cover universe #255 uses.
  4. Candidates are sorted safest-to-cancel (lowest impact) first.
  5. No owned services -> no candidates, no crash.
  6. `/streaming` renders the new section end-to-end for a logged-in user, with
     real anime-id-linked title chips (#271 precedent).
"""

import json
import os
import sys
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


def _insert_anime(pg_conn, anime_id, sites, title=None, episodes=None):
    links = [{"site": s, "url": f"https://example.com/{anime_id}"} for s in sites]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, external_links, episodes) VALUES (%s, %s, %s::jsonb, %s)",
            (anime_id, title or f"Anime {anime_id}", json.dumps(links), episodes),
        )


def _insert_entry(pg_conn, anime_id, status, user_id=USER_ID, progress=0):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress) VALUES (%s, %s, %s, %s)",
            (user_id, anime_id, status, progress),
        )


def _set_owned(pg_conn, services, user_id=USER_ID):
    with pg_conn.cursor() as cur:
        for s in services:
            cur.execute(
                "INSERT INTO user_streaming_services (user_id, service) VALUES (%s, %s)",
                (user_id, s),
            )


def _seed_realistic_library(pg_conn):
    """
      T1 WATCHING  Crunchyroll + Netflix   (Netflix redundant w/ Crunchyroll here)
      T2 WATCHING  Crunchyroll             (Crunchyroll-unique)
      T3 PLANNING  Hulu                    (Hulu-unique)
      T4 PLANNING  no recognized site at all — still counts toward the denominator
      T5 COMPLETED Crunchyroll             (excluded — not WATCHING/PLANNING)

    Owned: Crunchyroll, Netflix, Hulu.
    Watching/Planning total = 4 (T1-T4). T5 must not affect the denominator.
      Netflix: unique coverage = {} -> count 0, pct 0.0, fully_redundant True.
      Crunchyroll: unique coverage = {T2} -> count 1, pct 25.0.
      Hulu: unique coverage = {T3} -> count 1, pct 25.0.
    """
    _insert_anime(pg_conn, 1, ["Crunchyroll", "Netflix"], title="T1")
    _insert_entry(pg_conn, 1, "WATCHING")
    _insert_anime(pg_conn, 2, ["Crunchyroll"], title="T2")
    _insert_entry(pg_conn, 2, "WATCHING")
    _insert_anime(pg_conn, 3, ["Hulu"], title="T3")
    _insert_entry(pg_conn, 3, "PLANNING")
    _insert_anime(pg_conn, 4, ["SomeRandomWiki"], title="T4")
    _insert_entry(pg_conn, 4, "PLANNING")
    _insert_anime(pg_conn, 5, ["Crunchyroll"], title="T5")
    _insert_entry(pg_conn, 5, "COMPLETED")

    _set_owned(pg_conn, ["Crunchyroll", "Netflix", "Hulu"])


def test_cancel_candidates_denominator_is_watching_planning_only(
    pg_conn, app_module, _seeded_user
):
    _seed_realistic_library(pg_conn)

    result = app_module._compute_streaming_cancel_candidates(USER_ID)

    # T1-T4 only — T5 (COMPLETED) excluded even though it has a streaming link.
    assert result["total_titles"] == 4


def test_cancel_candidates_fully_redundant_service_flagged(
    pg_conn, app_module, _seeded_user
):
    _seed_realistic_library(pg_conn)

    result = app_module._compute_streaming_cancel_candidates(USER_ID)
    by_service = {c["service"]: c for c in result["candidates"]}

    netflix = by_service["Netflix"]
    assert netflix["count"] == 0
    assert netflix["pct"] == 0.0
    assert netflix["titles"] == []
    assert netflix["fully_redundant"] is True


def test_cancel_candidates_unique_coverage_reports_count_pct_and_titles(
    pg_conn, app_module, _seeded_user
):
    _seed_realistic_library(pg_conn)

    result = app_module._compute_streaming_cancel_candidates(USER_ID)
    by_service = {c["service"]: c for c in result["candidates"]}

    crunchyroll = by_service["Crunchyroll"]
    assert crunchyroll["count"] == 1
    assert crunchyroll["pct"] == 25.0
    assert crunchyroll["fully_redundant"] is False
    assert crunchyroll["titles"] == [{"id": 2, "title": "T2", "status": "WATCHING"}]

    hulu = by_service["Hulu"]
    assert hulu["count"] == 1
    assert hulu["pct"] == 25.0
    assert hulu["titles"] == [{"id": 3, "title": "T3", "status": "PLANNING"}]


def test_cancel_candidates_sorted_safest_first(pg_conn, app_module, _seeded_user):
    _seed_realistic_library(pg_conn)

    result = app_module._compute_streaming_cancel_candidates(USER_ID)
    services_in_order = [c["service"] for c in result["candidates"]]

    # Netflix (0 impact) must come before Crunchyroll/Hulu (1 impact each, tied
    # -> alphabetical). Deterministic full ordering.
    assert services_in_order == ["Netflix", "Crunchyroll", "Hulu"]


def test_cancel_candidates_weighted_by_episodes_remaining_not_title_count(
    pg_conn, app_module, _seeded_user
):
    """Issue #285: a single long-runner uniquely on one service must outweigh
    several short shows uniquely on another service — the headline `episodes`/
    `episodes_pct` fields must reflect that even though `count` (title-based,
    secondary) looks similar or reversed."""
    # Crunchyroll-unique: one 500-episode long-runner, 10 watched -> 490 remaining.
    _insert_anime(pg_conn, 1, ["Crunchyroll"], title="LongRunner", episodes=500)
    _insert_entry(pg_conn, 1, "WATCHING", progress=10)
    # Hulu-unique: three short 12-episode shows, untouched -> 12 remaining each = 36.
    _insert_anime(pg_conn, 2, ["Hulu"], title="Short1", episodes=12)
    _insert_entry(pg_conn, 2, "WATCHING", progress=0)
    _insert_anime(pg_conn, 3, ["Hulu"], title="Short2", episodes=12)
    _insert_entry(pg_conn, 3, "WATCHING", progress=0)
    _insert_anime(pg_conn, 4, ["Hulu"], title="Short3", episodes=12)
    _insert_entry(pg_conn, 4, "WATCHING", progress=0)
    _set_owned(pg_conn, ["Crunchyroll", "Hulu"])

    result = app_module._compute_streaming_cancel_candidates(USER_ID)
    by_service = {c["service"]: c for c in result["candidates"]}

    crunchyroll = by_service["Crunchyroll"]
    hulu = by_service["Hulu"]

    # Title count alone would rank Hulu as higher-impact (3 titles vs 1) — but
    # episode-weighted, Crunchyroll's single long-runner is the real bigger loss.
    assert crunchyroll["count"] == 1
    assert hulu["count"] == 3
    assert crunchyroll["episodes"] == 490
    assert hulu["episodes"] == 36
    assert crunchyroll["episodes"] > hulu["episodes"]
    assert result["total_episodes_remaining"] == 490 + 36
    assert crunchyroll["episodes_pct"] == round(490 / 526 * 100, 1)

    # Sort order (safest-to-cancel first) must follow the episode-weighted
    # headline, not the title count — Hulu (36 remaining) now sorts before
    # Crunchyroll (490 remaining) even though Crunchyroll has fewer titles.
    services_in_order = [c["service"] for c in result["candidates"]]
    assert services_in_order == ["Hulu", "Crunchyroll"]


def test_cancel_candidates_null_episodes_falls_back_without_crashing(
    pg_conn, app_module, _seeded_user
):
    """Issue #285 acceptance criteria: a title with anime.episodes NULL (e.g.
    still-airing with no confirmed final count) must not crash the calculation
    or silently drop out of the episode-weighted denominator — it's treated as
    1 remaining episode, same fallback _episodes_remaining already uses for
    #182's marginal-value ranking."""
    _insert_anime(pg_conn, 1, ["Crunchyroll"], title="StillAiring", episodes=None)
    _insert_entry(pg_conn, 1, "WATCHING", progress=3)
    _set_owned(pg_conn, ["Crunchyroll"])

    result = app_module._compute_streaming_cancel_candidates(USER_ID)

    assert result["total_episodes_remaining"] == 1  # fallback weight, not a crash/0
    crunchyroll = result["candidates"][0]
    assert crunchyroll["count"] == 1
    assert crunchyroll["episodes"] == 1
    assert crunchyroll["episodes_pct"] == 100.0
    assert crunchyroll["fully_redundant"] is False


def test_cancel_candidates_no_owned_services_produces_empty_list(pg_conn, app_module, _seeded_user):
    _insert_anime(pg_conn, 1, ["Netflix"], title="Solo")
    _insert_entry(pg_conn, 1, "PLANNING")

    result = app_module._compute_streaming_cancel_candidates(USER_ID)

    assert result["candidates"] == []
    assert result["total_titles"] == 1


def test_cancel_candidates_empty_watching_planning_list_no_division_by_zero(
    pg_conn, app_module, _seeded_user
):
    _set_owned(pg_conn, ["Crunchyroll"])

    result = app_module._compute_streaming_cancel_candidates(USER_ID)

    assert result["total_titles"] == 0
    assert result["total_episodes_remaining"] == 0
    assert result["candidates"] == [
        {
            "service": "Crunchyroll",
            "count": 0,
            "pct": 0.0,
            "episodes": 0,
            "episodes_pct": 0.0,
            "titles": [],
            "fully_redundant": True,
        }
    ]


# ── /streaming end-to-end render ─────────────────────────────────────────────

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


def test_streaming_page_renders_cancel_candidates_section(pg_conn, app_module, client):
    _register_and_login(client)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = 'owner@example.com'")
        (uid,) = cur.fetchone()

    _insert_anime(pg_conn, 1, ["Crunchyroll", "Netflix"], title="Overlap Title")
    _insert_entry(pg_conn, 1, "WATCHING", user_id=uid)
    _insert_anime(pg_conn, 2, ["Hulu"], title="Hulu Exclusive")
    _insert_entry(pg_conn, 2, "PLANNING", user_id=uid)
    _set_owned(pg_conn, ["Crunchyroll", "Netflix", "Hulu"], user_id=uid)

    resp = client.get("/streaming")
    assert resp.status_code == 200

    # Section heading present.
    assert "Cancel candidates" in resp.text
    # Netflix's cancel impact is zero -> "safe to cancel" badge must appear.
    assert "Safe to cancel" in resp.text
    # Hulu's unique title must be linked through to its real anime id.
    assert '<a href="/anime/2/notes?back=PLANNING"' in resp.text
    assert "Hulu Exclusive" in resp.text


def test_streaming_page_cancel_section_handles_no_owned_services(pg_conn, app_module, client):
    _register_and_login(client, email="fresh@example.com")

    resp = client.get("/streaming")
    assert resp.status_code == 200
    assert "Cancel candidates" in resp.text
    assert "Set which streaming services you own in Settings" in resp.text
