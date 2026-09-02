"""
Coverage for issue #307 — search-result cards had no way to navigate into an
anime's own notes/detail page. Library's cards get this from a JS click
delegation keyed on a #notes-modal element the search page doesn't have, so it
never attached there; the fix wraps the cover image and title in a plain
<a href="/anime/{id}/notes?back={status}"> instead, since this page doesn't
need library's quick-edit modal.

Verified against a real Postgres, same pattern as tests/test_notes_search.py:
a real FastAPI TestClient driving the actual GET /search route.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM users")


def _register_and_login(client, email="owner@example.com", password="correct horse battery staple"):
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def _current_user_id(pg_conn, email):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        return cur.fetchone()[0]


def _insert_anime(pg_conn, anime_id, title, cover_image_url=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, cover_image_url) VALUES (%s, %s, %s)",
            (anime_id, title, cover_image_url),
        )


def _insert_entry(pg_conn, user_id, anime_id, status="WATCHING"):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status) VALUES (%s, %s, %s)",
            (user_id, anime_id, status),
        )


def test_search_result_title_links_to_notes_page(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")
    _insert_anime(pg_conn, 1, "Naruto")
    _insert_entry(pg_conn, uid, 1, status="COMPLETED")

    resp = client.get("/search", params={"q": "naruto"})
    assert resp.status_code == 200
    assert 'href="/anime/1/notes?back=COMPLETED"' in resp.text


def test_search_result_cover_image_also_links_to_notes_page(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")
    _insert_anime(pg_conn, 1, "Naruto", cover_image_url="https://example.invalid/cover.jpg")
    _insert_entry(pg_conn, uid, 1, status="WATCHING")

    resp = client.get("/search", params={"q": "naruto"})
    assert resp.status_code == 200
    # Both the cover-image link and the title link point at the same
    # destination — two separate <a> wrappers, not one shared/nested anchor.
    assert resp.text.count('href="/anime/1/notes?back=WATCHING"') == 2


def test_search_result_still_has_working_library_and_anilist_links(pg_conn, app_module, client):
    """The two pre-existing links (back-to-library, external AniList) must keep
    working exactly as before — this fix adds a new link, it doesn't replace
    what was already there."""
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")
    _insert_anime(pg_conn, 1, "Naruto")
    _insert_entry(pg_conn, uid, 1, status="WATCHING")

    resp = client.get("/search", params={"q": "naruto"})
    assert resp.status_code == 200
    assert 'href="/?status=WATCHING#card-1"' in resp.text
    assert 'href="https://anilist.co/anime/1"' in resp.text


def test_multiple_search_results_each_link_to_their_own_anime(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")
    _insert_anime(pg_conn, 1, "Naruto")
    _insert_anime(pg_conn, 2, "Naruto: Shippuden")
    _insert_entry(pg_conn, uid, 1, status="COMPLETED")
    _insert_entry(pg_conn, uid, 2, status="WATCHING")

    resp = client.get("/search", params={"q": "naruto"})
    assert resp.status_code == 200
    assert 'href="/anime/1/notes?back=COMPLETED"' in resp.text
    assert 'href="/anime/2/notes?back=WATCHING"' in resp.text
