"""
Coverage for issue #302 — /upcoming's filler-episode tag on List/Week-view entry
cards and the recolored Month-view chip dot, sourced from #299's
filler_episode_cache table.

Verified against a real Postgres, same pattern as tests/test_upcoming_month_grid.py
and tests/test_upcoming_week_grid.py — skipped entirely if one isn't reachable via
DATABASE_URL.

Covers the acceptance criteria from #302:
  1. A List-view entry whose specific episode has a known 'filler' status shows a
     badge naming it.
  2. A Month-view chip for a known-filler episode shows the recolored dot; other
     statuses (canon/mixed) and an unmatched episode (unknown) keep the default
     dot/no badge — a single clear signal, not a second color meaning something
     subtly different.
  3. Neither surface shows anything when the specific episode has no
     filler_episode_cache row at all (unknown) — no placeholder, no guess.
  4. The Week grid view (which reuses the same entry_card macro as List view)
     picks up the same badge for free.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM filler_episode_cache")
        cur.execute("DELETE FROM airing_schedule_cache")
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM users")


def _insert_anime(pg_conn, anime_id, title, episodes=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, episodes) VALUES (%s, %s, %s)",
            (anime_id, title, episodes),
        )


def _insert_entry(pg_conn, anime_id, user_id, status="WATCHING", progress=0):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, anime_id, status, progress),
        )


def _insert_airing(pg_conn, anime_id, episode, airing_at):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO airing_schedule_cache (anime_id, episode, airing_at) VALUES (%s, %s, %s)",
            (anime_id, episode, airing_at),
        )


def _insert_filler_status(pg_conn, anime_id, episode_number, status):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO filler_episode_cache (anime_id, episode_number, status) "
            "VALUES (%s, %s, %s)",
            (anime_id, episode_number, status),
        )


def _register_and_login(client, email="owner@example.com", password="correct horse battery staple"):
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def _user_id(pg_conn, email):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        return cur.fetchone()[0]


def _list_slice(html):
    idx = html.index('id="upcoming-list-view"')
    end = html.index('id="upcoming-grid-view"')
    return html[idx:end]


def _grid_slice(html):
    idx = html.index('id="upcoming-grid-view"')
    end = html.index('id="upcoming-month-view"')
    return html[idx:end]


def _month_slice(html):
    idx = html.index('id="upcoming-month-view"')
    return html[idx:]


def _now():
    return datetime.now(timezone.utc)


# ── 1. List view: badge shows for a known 'filler' episode ──────────────────

def test_list_view_shows_filler_badge_for_known_filler_episode(pg_conn, app_module, client):
    _register_and_login(client, email="filler@example.com")
    uid = _user_id(pg_conn, "filler@example.com")
    _insert_anime(pg_conn, 1, "Filler Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 5, _now() + timedelta(hours=2))
    _insert_filler_status(pg_conn, 1, 5, "filler")

    resp = client.get("/upcoming")
    assert resp.status_code == 200
    list_html = _list_slice(resp.text)
    assert "Filler Show" in list_html
    assert "filler-badge" in list_html


def test_list_view_no_badge_for_canon_episode(pg_conn, app_module, client):
    _register_and_login(client, email="canon@example.com")
    uid = _user_id(pg_conn, "canon@example.com")
    _insert_anime(pg_conn, 1, "Canon Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 5, _now() + timedelta(hours=2))
    _insert_filler_status(pg_conn, 1, 5, "canon")

    resp = client.get("/upcoming")
    list_html = _list_slice(resp.text)
    assert "Canon Show" in list_html
    assert "filler-badge" not in list_html


def test_list_view_no_badge_for_mixed_episode(pg_conn, app_module, client):
    """#302 explicitly requires 'mixed' to render identically to today — no
    second visual signal for it."""
    _register_and_login(client, email="mixed@example.com")
    uid = _user_id(pg_conn, "mixed@example.com")
    _insert_anime(pg_conn, 1, "Mixed Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 5, _now() + timedelta(hours=2))
    _insert_filler_status(pg_conn, 1, 5, "mixed")

    resp = client.get("/upcoming")
    list_html = _list_slice(resp.text)
    assert "Mixed Show" in list_html
    assert "filler-badge" not in list_html


def test_list_view_no_badge_when_status_unknown(pg_conn, app_module, client):
    """No filler_episode_cache row at all for this (anime_id, episode) — absence
    means unknown, not canon, and unknown must render with no badge (no
    placeholder, no guess)."""
    _register_and_login(client, email="unknown@example.com")
    uid = _user_id(pg_conn, "unknown@example.com")
    _insert_anime(pg_conn, 1, "Unknown Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 5, _now() + timedelta(hours=2))
    # Cache has a row for a *different* episode of the same anime, to prove the
    # lookup is scoped to the specific airing episode, not just the anime.
    _insert_filler_status(pg_conn, 1, 4, "filler")

    resp = client.get("/upcoming")
    list_html = _list_slice(resp.text)
    assert "Unknown Show" in list_html
    assert "filler-badge" not in list_html


# ── 2. Month view: dot recolors only for a known-filler episode ─────────────

def test_month_view_dot_recolors_for_filler_episode(pg_conn, app_module, client):
    _register_and_login(client, email="monthfiller@example.com")
    uid = _user_id(pg_conn, "monthfiller@example.com")
    _insert_anime(pg_conn, 1, "Month Filler Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 5, _now() + timedelta(hours=2))
    _insert_filler_status(pg_conn, 1, 5, "filler")

    resp = client.get("/upcoming")
    month_html = _month_slice(resp.text)
    assert "Month Filler Show" in month_html
    assert "upcoming-month-chip-dot--filler" in month_html


def test_month_view_dot_default_for_canon_mixed_and_unknown(pg_conn, app_module, client):
    _register_and_login(client, email="monthdefault@example.com")
    uid = _user_id(pg_conn, "monthdefault@example.com")

    _insert_anime(pg_conn, 1, "Canon Month Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 1, _now() + timedelta(hours=1))
    _insert_filler_status(pg_conn, 1, 1, "canon")

    _insert_anime(pg_conn, 2, "Mixed Month Show")
    _insert_entry(pg_conn, 2, uid)
    _insert_airing(pg_conn, 2, 1, _now() + timedelta(hours=2))
    _insert_filler_status(pg_conn, 2, 1, "mixed")

    _insert_anime(pg_conn, 3, "Unknown Month Show")
    _insert_entry(pg_conn, 3, uid)
    _insert_airing(pg_conn, 3, 1, _now() + timedelta(hours=3))
    # No filler_episode_cache row at all for anime 3.

    resp = client.get("/upcoming")
    month_html = _month_slice(resp.text)
    assert "Canon Month Show" in month_html
    assert "Mixed Month Show" in month_html
    assert "Unknown Month Show" in month_html
    assert "upcoming-month-chip-dot--filler" not in month_html


# ── 3. Week grid view reuses the same entry_card macro ───────────────────────

def test_week_grid_view_also_shows_filler_badge(pg_conn, app_module, client):
    """Week view (#256's grid) reuses the exact same entry_card() macro as List
    view, so #302's badge extends there automatically without any layout change
    — this locks that in as a regression guard."""
    _register_and_login(client, email="weekfiller@example.com")
    uid = _user_id(pg_conn, "weekfiller@example.com")
    _insert_anime(pg_conn, 1, "Week Filler Show")
    _insert_entry(pg_conn, 1, uid)
    _insert_airing(pg_conn, 1, 5, _now() + timedelta(hours=2))
    _insert_filler_status(pg_conn, 1, 5, "filler")

    resp = client.get("/upcoming")
    grid_html = _grid_slice(resp.text)
    assert "Week Filler Show" in grid_html
    assert "filler-badge" in grid_html
