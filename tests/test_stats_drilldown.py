"""
Coverage for issue #225 — Stats: click-through drill-down on stat cards.

Verified against a real Postgres, same pattern as tests/test_collections.py: a
real FastAPI TestClient driving the actual HTTP routes against the real
schema.sql shape. No schema change was needed for this issue — these tests are
partly here to prove that (schema.sql applies as-is, no migrations/ file
added).

What's actually testable from pytest (server-side contract only — the click
handling itself is client-side JS replayed through script.js's applyFromUrl,
same "nothing for pytest to exercise there" note test_collections.py makes
about Collections' own replay mechanism; that side was verified live instead,
see the PR description):

  1. `GET /?status=ALL` (the new pseudo-status the drill-down links for
     cross-status headlines/score-chart use) returns entries from every
     status, not just one — and existing single-status values are completely
     unaffected (regression coverage for the query shape change in
     app/main.py's library() route).
  2. library.html renders *every* genre for a card, not just the first 4 shown
     — the extra ones carry `hidden` so the free-text search the genre-chart
     drill-down relies on (see stats.html) can match an anime whose only
     matching genre is its 5th+, not just what's visually displayed.
  3. /stats renders the new headline drill-down links with the correct
     `/?status=...` targets, and the discoverability hint text.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extras import Json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM invites")
        cur.execute("DELETE FROM users")


def _register_and_login(client, email="owner@example.com", password="correct horse battery staple"):
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def _user_id(pg_conn, email="owner@example.com"):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        return cur.fetchone()[0]


def _seed_anime(pg_conn, anime_id, title, genres, user_id, status, score=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO anime (id, title_romaji, title_english, genres, episodes, duration)
            VALUES (%s, %s, %s, %s, 12, 24)
            ON CONFLICT (id) DO UPDATE SET genres = EXCLUDED.genres
            """,
            (anime_id, title, title, Json(genres)),
        )
        cur.execute(
            """
            INSERT INTO library_entries (user_id, anime_id, status, score, progress)
            VALUES (%s, %s, %s, %s, 12)
            """,
            (user_id, anime_id, status, score),
        )


# ── 1. status=ALL spans every status; existing single-status values unaffected ──

def test_status_all_returns_entries_from_every_status(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _user_id(pg_conn)
    _seed_anime(pg_conn, 1, "Watching Show", ["Action"], uid, "WATCHING")
    _seed_anime(pg_conn, 2, "Completed Show", ["Drama"], uid, "COMPLETED")
    _seed_anime(pg_conn, 3, "Dropped Show", ["Comedy"], uid, "DROPPED")

    resp = client.get("/?status=ALL")
    assert resp.status_code == 200
    assert "Watching Show" in resp.text
    assert "Completed Show" in resp.text
    assert "Dropped Show" in resp.text


def test_status_all_is_case_insensitive(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _user_id(pg_conn)
    _seed_anime(pg_conn, 1, "Lowercase All Show", ["Action"], uid, "WATCHING")

    resp = client.get("/?status=all")
    assert resp.status_code == 200
    assert "Lowercase All Show" in resp.text


def test_status_all_scoped_to_requesting_user_only(pg_conn, app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as client_a:
        _register_and_login(client_a, email="user-a@example.com")
        uid_a = _user_id(pg_conn, "user-a@example.com")
        _seed_anime(pg_conn, 1, "A's Show", ["Action"], uid_a, "WATCHING")

    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO invites (email) VALUES (%s)", ("user-b@example.com",))
    with TestClient(app_module.app) as client_b:
        _register_and_login(client_b, email="user-b@example.com")
        resp = client_b.get("/?status=ALL")
        assert resp.status_code == 200
        assert "A's Show" not in resp.text


def test_single_status_values_still_scope_correctly_after_all_support_added(pg_conn, app_module, client):
    """Regression check on the library() query-building change: a real status
    value must still filter to exactly that status, not accidentally fall
    through to the ALL branch or pick up other statuses' rows."""
    _register_and_login(client)
    uid = _user_id(pg_conn)
    _seed_anime(pg_conn, 1, "Only Watching", ["Action"], uid, "WATCHING")
    _seed_anime(pg_conn, 2, "Only Completed", ["Drama"], uid, "COMPLETED")

    resp = client.get("/?status=COMPLETED")
    assert resp.status_code == 200
    assert "Only Completed" in resp.text
    assert "Only Watching" not in resp.text

    resp = client.get("/?status=WATCHING")
    assert resp.status_code == 200
    assert "Only Watching" in resp.text
    assert "Only Completed" not in resp.text


def test_status_all_requires_auth(client):
    resp = client.get("/?status=ALL", follow_redirects=False)
    assert resp.status_code in (302, 303, 401)


# ── 2. Full genre list rendered (visible first 4 + hidden overflow) ────────────

def test_library_card_renders_genres_beyond_first_four_as_hidden(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _user_id(pg_conn)
    genres = ["Action", "Adventure", "Comedy", "Drama", "Fantasy", "Mecha"]
    _seed_anime(pg_conn, 1, "Many Genres Show", genres, uid, "WATCHING")

    resp = client.get("/?status=WATCHING")
    assert resp.status_code == 200
    # All six genres must appear somewhere in the card's markup...
    for g in genres:
        assert f'<span class="genre"' in resp.text
        assert g in resp.text
    # ...and the 5th/6th (beyond the visible cap) must be marked hidden so they
    # don't show on-card but still participate in the free-text search that
    # the /stats genre-chart drill-down (issue #225) relies on.
    assert '<span class="genre" hidden>Fantasy</span>' in resp.text
    assert '<span class="genre" hidden>Mecha</span>' in resp.text
    # The first four must NOT be hidden (still visibly capped at 4, unchanged
    # display behavior).
    assert '<span class="genre">Action</span>' in resp.text
    assert '<span class="genre">Drama</span>' in resp.text


def test_library_card_with_four_or_fewer_genres_has_no_hidden_chips(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _user_id(pg_conn)
    _seed_anime(pg_conn, 1, "Few Genres Show", ["Action", "Comedy"], uid, "WATCHING")

    resp = client.get("/?status=WATCHING")
    assert resp.status_code == 200
    assert '<span class="genre" hidden>' not in resp.text


# ── 3. /stats renders the drill-down links and hint ─────────────────────────────

def test_stats_page_renders_headline_drilldown_links(client):
    _register_and_login(client)
    resp = client.get("/stats")
    assert resp.status_code == 200
    assert 'href="/?status=COMPLETED"' in resp.text
    assert 'href="/?status=WATCHING"' in resp.text
    assert 'href="/?status=ALL"' in resp.text
    assert resp.text.count('href="/?status=ALL"') == 2  # episodes + watch time headlines


def test_stats_page_renders_drilldown_hint_text(client):
    _register_and_login(client)
    resp = client.get("/stats")
    assert resp.status_code == 200
    assert "Click a number, bar, or slice to see the anime behind it." in resp.text
