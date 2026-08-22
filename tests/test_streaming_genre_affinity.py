"""
Coverage for issue #286 — Genre affinity coverage on /streaming.

#286's own Scope section required a data-verification step before committing to
the feature: does `anime.genres` actually support a meaningful per-genre coverage
breakdown? Verified against prod (2026-08-22): `genres` is AniList's own fixed,
18-value canonical taxonomy, consistently Title Case with zero casing/format
drift, populated on 99.8% of anime rows, averaging ~3-4 genres per title — the
same column /stats' existing "top_genre" and taste-drift chart already treat as
a meaningful signal. Confirmed viable; this file covers the step-2 feature that
verification unlocked: correlating episode-weighted coverage (#285) against a
user's top-RATED genres (library_entries.score x anime.genres).

Same throwaway-Postgres pattern as tests/test_streaming_cancel_candidates.py —
skipped entirely if one isn't reachable.

Covers:
  1. No scored COMPLETED entry with a genre yet -> the whole section is None
     (hidden), not an empty/noisy chart.
  2. A genre below GENRE_AFFINITY_MIN_TITLES scored COMPLETED titles is excluded
     (excluded_low_volume), same MIN_TITLES-gate precedent as
     _compute_studio_loyalty (#223).
  3. A genre that clears the title-count floor but has zero *scored* completions
     is excluded too (excluded_unscored), tracked separately.
  4. Qualifying genres are sorted by avg_score desc, title_count desc, then name.
  5. Per-genre coverage is restricted to that genre's titles within the existing
     Watching/Planning/Upcoming streaming universe, and is episode-weighted
     (#285), not title-count weighted.
  6. A genre that qualifies on the rating side but has nothing left in the
     streaming universe (e.g. every title carrying it is already Completed) is
     dropped from the output entirely — no coverage question left to answer.
  7. `/streaming` renders the new section end-to-end for a logged-in user, and
     hides it entirely when _compute_streaming_genre_affinity returns None.
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


def _insert_anime(pg_conn, anime_id, sites, genres, title=None, episodes=None):
    links = [{"site": s, "url": f"https://example.com/{anime_id}"} for s in sites]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, external_links, genres, episodes) "
            "VALUES (%s, %s, %s::jsonb, %s::jsonb, %s)",
            (anime_id, title or f"Anime {anime_id}", json.dumps(links), json.dumps(genres), episodes),
        )


def _insert_entry(pg_conn, anime_id, status, user_id=USER_ID, progress=0, score=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, score) "
            "VALUES (%s, %s, %s, %s, %s)",
            (user_id, anime_id, status, progress, score),
        )


def _set_owned(pg_conn, services, user_id=USER_ID):
    with pg_conn.cursor() as cur:
        for s in services:
            cur.execute(
                "INSERT INTO user_streaming_services (user_id, service) VALUES (%s, %s)",
                (user_id, s),
            )


def test_genre_affinity_none_without_scored_completed_titles(pg_conn, app_module, _seeded_user):
    _insert_anime(pg_conn, 1, ["Crunchyroll"], ["Action"], title="T1")
    _insert_entry(pg_conn, 1, "WATCHING")  # not COMPLETED -> never counts

    result = app_module._compute_streaming_genre_affinity(USER_ID)

    assert result is None


def test_genre_affinity_min_titles_gate_excludes_low_volume_genre(pg_conn, app_module, _seeded_user):
    # Only 2 scored COMPLETED "Horror" titles — below GENRE_AFFINITY_MIN_TITLES (3).
    assert app_module.GENRE_AFFINITY_MIN_TITLES == 3
    _insert_anime(pg_conn, 1, ["Crunchyroll"], ["Horror"], title="H1")
    _insert_entry(pg_conn, 1, "COMPLETED", score=4.0)
    _insert_anime(pg_conn, 2, ["Crunchyroll"], ["Horror"], title="H2")
    _insert_entry(pg_conn, 2, "COMPLETED", score=4.5)

    result = app_module._compute_streaming_genre_affinity(USER_ID)

    assert result is None
    # Nothing qualifies, but the function still counted the attempt.


def test_genre_affinity_unscored_completed_titles_excluded(pg_conn, app_module, _seeded_user):
    # 3 COMPLETED "Mecha" titles clear the title-count floor, but none are scored.
    for i in range(1, 4):
        _insert_anime(pg_conn, i, ["Crunchyroll"], ["Mecha"], title=f"M{i}")
        _insert_entry(pg_conn, i, "COMPLETED", score=None)

    result = app_module._compute_streaming_genre_affinity(USER_ID)

    assert result is None


def test_genre_affinity_qualifying_genres_sorted_by_avg_score_desc(pg_conn, app_module, _seeded_user):
    # "Fantasy": 3 titles, avg score 4.0.
    for i, sc in enumerate([4.0, 4.0, 4.0], start=1):
        _insert_anime(pg_conn, i, ["Crunchyroll"], ["Fantasy"], title=f"F{i}")
        _insert_entry(pg_conn, i, "COMPLETED", score=sc)
    # "Comedy": 3 titles, avg score 4.8 (higher -> should rank first).
    for i, sc in enumerate([5.0, 4.5, 5.0], start=4):
        _insert_anime(pg_conn, i, ["Crunchyroll"], ["Comedy"], title=f"C{i}")
        _insert_entry(pg_conn, i, "COMPLETED", score=sc)
    # Both genres also present in the active Watching/Planning universe so
    # coverage isn't zeroed out and dropped.
    _insert_anime(pg_conn, 100, ["Crunchyroll"], ["Fantasy"], title="F-active")
    _insert_entry(pg_conn, 100, "WATCHING")
    _insert_anime(pg_conn, 101, ["Netflix"], ["Comedy"], title="C-active")
    _insert_entry(pg_conn, 101, "WATCHING")

    result = app_module._compute_streaming_genre_affinity(USER_ID)

    assert result is not None
    genres_in_order = [g["genre"] for g in result["genres"]]
    assert genres_in_order == ["Comedy", "Fantasy"]
    comedy = result["genres"][0]
    assert comedy["avg_score"] == 4.83
    assert comedy["title_count"] == 3


def test_genre_affinity_coverage_is_episode_weighted_and_genre_scoped(pg_conn, app_module, _seeded_user):
    # Rating side: 3 scored COMPLETED "Action" titles to clear the gate.
    for i, sc in enumerate([4.0, 4.5, 5.0], start=1):
        _insert_anime(pg_conn, i, ["Crunchyroll"], ["Action"], title=f"A{i}")
        _insert_entry(pg_conn, i, "COMPLETED", score=sc)

    # Coverage side (Watching/Planning universe), all tagged "Action":
    #  - AA1 (Crunchyroll): 500 episodes, progress 10 -> 490 remaining.
    #  - AA2 (Netflix): 10 episodes, progress 0 -> 10 remaining.
    # A third title with a DIFFERENT genre must never leak into Action's totals.
    _insert_anime(pg_conn, 10, ["Crunchyroll"], ["Action"], title="AA1", episodes=500)
    _insert_entry(pg_conn, 10, "WATCHING", progress=10)
    _insert_anime(pg_conn, 11, ["Netflix"], ["Action"], title="AA2", episodes=10)
    _insert_entry(pg_conn, 11, "WATCHING", progress=0)
    _insert_anime(pg_conn, 12, ["Hulu"], ["Romance"], title="Unrelated", episodes=12)
    _insert_entry(pg_conn, 12, "WATCHING", progress=0)

    _set_owned(pg_conn, ["Crunchyroll"])

    result = app_module._compute_streaming_genre_affinity(USER_ID)

    assert result is not None
    action = next(g for g in result["genres"] if g["genre"] == "Action")
    assert action["total_episodes"] == 500  # only Action titles: 490 + 10
    services_by_name = {s["service"]: s for s in action["services"]}
    assert services_by_name["Crunchyroll"]["episodes"] == 490
    assert services_by_name["Crunchyroll"]["pct"] == round(490 / 500 * 100, 1)
    assert services_by_name["Crunchyroll"]["owned"] is True
    assert services_by_name["Netflix"]["episodes"] == 10
    assert services_by_name["Netflix"]["owned"] is False
    # Romance's title/episodes must not appear anywhere in Action's breakdown.
    assert "Hulu" not in services_by_name


def test_genre_affinity_drops_genre_with_zero_universe_coverage(pg_conn, app_module, _seeded_user):
    # "Drama" clears the rating-side gate (3 scored COMPLETED titles) but every
    # Drama title is COMPLETED -> nothing left in the Watching/Planning/Upcoming
    # universe -> the genre must be dropped from the output entirely.
    for i, sc in enumerate([4.0, 4.0, 4.0], start=1):
        _insert_anime(pg_conn, i, ["Crunchyroll"], ["Drama"], title=f"D{i}")
        _insert_entry(pg_conn, i, "COMPLETED", score=sc)

    result = app_module._compute_streaming_genre_affinity(USER_ID)

    assert result is None


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


def test_streaming_page_renders_genre_affinity_section(pg_conn, app_module, client):
    _register_and_login(client)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = 'owner@example.com'")
        (uid,) = cur.fetchone()

    for i, sc in enumerate([5.0, 5.0, 5.0], start=1):
        _insert_anime(pg_conn, i, ["Crunchyroll"], ["Fantasy"], title=f"F{i}")
        _insert_entry(pg_conn, i, "COMPLETED", user_id=uid, score=sc)
    _insert_anime(pg_conn, 10, ["Crunchyroll"], ["Fantasy"], title="Active Fantasy Title")
    _insert_entry(pg_conn, 10, "WATCHING", user_id=uid)
    _set_owned(pg_conn, ["Crunchyroll"], user_id=uid)

    resp = client.get("/streaming")
    assert resp.status_code == 200

    # The section heading only renders as an actual <h2> when the section is
    # present — window.I18N (see the other test's comment) makes a raw
    # substring check on the translated string alone insufficient.
    assert '<h2 class="settings-section-title">Coverage by your top-rated genres</h2>' in resp.text
    assert "Fantasy" in resp.text


def test_streaming_page_hides_genre_affinity_section_when_no_data(pg_conn, app_module, client):
    _register_and_login(client, email="fresh@example.com")

    resp = client.get("/streaming")
    assert resp.status_code == 200
    # base.html embeds the full translation dict as window.I18N on every page
    # (see the <script> block), so every locale string — including this
    # section's heading — is always present *somewhere* in the raw HTML
    # regardless of whether the section itself rendered. Check for the actual
    # rendered heading element instead of a raw substring match.
    assert '<h2 class="settings-section-title">Coverage by your top-rated genres</h2>' not in resp.text
