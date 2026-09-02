"""
Coverage for issue #368 — a failed cover-art image load (a stale/404'd
AniList-sourced URL) had no fallback: the browser's native broken-image icon +
alt text spilled out of `.cover`/`.upcoming-cover`'s container, worst on
`/upcoming`'s compact 44px thumbnails. Fixed with `overflow: hidden` +
placeholder background on both classes (style.css), plus an `onerror` handler
on every `<img class="cover">`/`<img class="upcoming-cover">` that hides the
broken image so the placeholder background shows through instead.

Verified against a real Postgres, same pattern as tests/test_issue_307_search_card_click.py.
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
        "/auth/register", data={"email": email, "password": password}, follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def _current_user_id(pg_conn, email):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        return cur.fetchone()[0]


def _insert_anime(pg_conn, anime_id, title, cover_image_url):
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


ONERROR_ATTR = 'onerror="this.onerror=null;this.style.opacity=\'0\';"'


def test_library_cover_has_onerror_fallback(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")
    _insert_anime(pg_conn, 1, "Naruto", cover_image_url="https://example.invalid/dead.jpg")
    _insert_entry(pg_conn, uid, 1, status="WATCHING")

    resp = client.get("/")
    assert resp.status_code == 200
    assert 'class="cover"' in resp.text
    assert ONERROR_ATTR in resp.text


def test_upcoming_cover_has_onerror_fallback(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")
    _insert_anime(pg_conn, 1, "Naruto", cover_image_url="https://example.invalid/dead.jpg")
    _insert_entry(pg_conn, uid, 1, status="WATCHING")
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO airing_schedule_cache (anime_id, episode, airing_at) "
            "VALUES (1, 5, now() + interval '1 day')"
        )

    resp = client.get("/upcoming")
    assert resp.status_code == 200
    assert 'class="upcoming-cover"' in resp.text
    assert ONERROR_ATTR in resp.text


def test_search_cover_has_onerror_fallback(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")
    _insert_anime(pg_conn, 1, "Naruto", cover_image_url="https://example.invalid/dead.jpg")
    _insert_entry(pg_conn, uid, 1, status="WATCHING")

    resp = client.get("/search", params={"q": "naruto"})
    assert resp.status_code == 200
    assert 'class="cover"' in resp.text
    assert ONERROR_ATTR in resp.text


def test_missing_cover_url_still_renders_no_img_tag(pg_conn, app_module, client):
    """A NULL cover_image_url is a different case from a failed load — the
    template already guards this with `{% if e.cover_image_url %}`, so no
    <img> (and therefore no onerror handler) should render at all. This fix
    shouldn't change that existing behavior."""
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")
    _insert_anime(pg_conn, 1, "Naruto", cover_image_url=None)
    _insert_entry(pg_conn, uid, 1, status="WATCHING")

    resp = client.get("/")
    assert resp.status_code == 200
    assert '<img' not in resp.text.split('data-anime-id="1"')[1].split("</li>")[0]
