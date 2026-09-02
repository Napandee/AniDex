"""
Coverage for issue #479 — /queue gains a 4th tab, WATCHING, so a user can
answer "which of my in-progress shows do I pick up right now" without
falling back to manually filtering the full Library view.

Verified against a real Postgres, same pattern as tests/test_notes_search.py:
a real FastAPI TestClient driving the actual GET /queue route against the
real schema.sql shape.

Covers the acceptance criteria:
  1. A user with multiple WATCHING entries sees all of them from /queue,
     ordered most-recently-progressed first (anilist_updated_at DESC).
  2. Reaching this view doesn't require the full Library page.
  3. The existing ALL/PLANNING/PAUSED tabs and their ordering are unchanged.

Plus the two behavioral guards this issue's implementation added:
  - WATCHING entries are not draggable and get no drag handle (drag-to-
    reorder persists watch_next_priority via /api/queue/reorder, which is a
    queue-prioritization signal that doesn't apply to an already-in-progress
    entry).
  - WATCHING entries carry data-notes-back="WATCHING" instead of the
    existing tabs' hardcoded "PLANNING", so the notes page's back-link
    returns to the right tab.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM personal_notes")
        cur.execute("DELETE FROM recommendation_scores")
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


def _insert_anime(pg_conn, anime_id, title):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, episodes) VALUES (%s, %s, %s)",
            (anime_id, title, 24),
        )


def _insert_entry(pg_conn, user_id, anime_id, status, anilist_updated_at=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO library_entries (user_id, anime_id, status, progress, anilist_updated_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (user_id, anime_id, status, 3, anilist_updated_at),
        )


def _item_html(body: str, anime_id: int) -> str:
    marker = f'data-anime-id="{anime_id}"'
    start = body.index(marker)
    item_start = body.rindex('<li class="list-row queue-item"', 0, start)
    next_item = body.find('<li class="list-row queue-item"', start)
    end = next_item if next_item != -1 else body.index("</ol>", start)
    return body[item_start:end]


# ── 1. WATCHING tab shows only WATCHING entries, most-recent first ─────────────

def test_watching_tab_shows_only_watching_entries(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _user_id(pg_conn)
    _insert_anime(pg_conn, 1, "Watching Show")
    _insert_anime(pg_conn, 2, "Planning Show")
    _insert_entry(pg_conn, uid, 1, "WATCHING")
    _insert_entry(pg_conn, uid, 2, "PLANNING")

    resp = client.get("/queue", params={"status": "WATCHING"})
    assert resp.status_code == 200
    assert 'data-anime-id="1"' in resp.text
    assert 'data-anime-id="2"' not in resp.text


def test_watching_tab_orders_most_recently_progressed_first(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _user_id(pg_conn)
    now = datetime.now(timezone.utc)
    _insert_anime(pg_conn, 1, "Stale Progress Show")
    _insert_anime(pg_conn, 2, "Fresh Progress Show")
    _insert_anime(pg_conn, 3, "No Timestamp Show")
    _insert_entry(pg_conn, uid, 1, "WATCHING", anilist_updated_at=now - timedelta(days=30))
    _insert_entry(pg_conn, uid, 2, "WATCHING", anilist_updated_at=now - timedelta(hours=1))
    _insert_entry(pg_conn, uid, 3, "WATCHING", anilist_updated_at=None)

    resp = client.get("/queue", params={"status": "WATCHING"})
    assert resp.status_code == 200
    pos_fresh = resp.text.index('data-anime-id="2"')
    pos_stale = resp.text.index('data-anime-id="1"')
    pos_none = resp.text.index('data-anime-id="3"')
    # Most-recently-progressed first; NULLS LAST puts the untimestamped entry
    # after both real timestamps, not first.
    assert pos_fresh < pos_stale < pos_none


def test_watching_status_added_to_tab_list(pg_conn, app_module, client):
    _register_and_login(client)
    resp = client.get("/queue")
    assert resp.status_code == 200
    assert 'href="/queue?status=WATCHING"' in resp.text


# ── 2. Reachable without falling back to the Library page ──────────────────────

def test_watching_tab_reachable_directly_from_queue_page(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _user_id(pg_conn)
    _insert_anime(pg_conn, 1, "In Progress Show")
    _insert_entry(pg_conn, uid, 1, "WATCHING")

    resp = client.get("/queue?status=WATCHING")
    assert resp.status_code == 200
    assert "In Progress Show" in resp.text


# ── 3. Existing ALL/PLANNING/PAUSED tabs and ordering are unchanged ────────────

def test_all_tab_still_excludes_watching(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _user_id(pg_conn)
    _insert_anime(pg_conn, 1, "Watching Show")
    _insert_anime(pg_conn, 2, "Planning Show")
    _insert_entry(pg_conn, uid, 1, "WATCHING")
    _insert_entry(pg_conn, uid, 2, "PLANNING")

    resp = client.get("/queue")
    assert resp.status_code == 200
    assert 'data-anime-id="2"' in resp.text
    assert 'data-anime-id="1"' not in resp.text


def test_planning_tab_ordering_unaffected_by_anilist_updated_at(pg_conn, app_module, client):
    """PLANNING/PAUSED ordering is priority/rec-score/title — never recency.
    A PLANNING entry with a *newer* anilist_updated_at than another must NOT
    be reordered ahead of it on that basis; alphabetical title is the
    tiebreaker once priority/rec_score are both absent for both rows."""
    _register_and_login(client)
    uid = _user_id(pg_conn)
    now = datetime.now(timezone.utc)
    _insert_anime(pg_conn, 1, "Zeta Show")
    _insert_anime(pg_conn, 2, "Alpha Show")
    _insert_entry(pg_conn, uid, 1, "PLANNING", anilist_updated_at=now)
    _insert_entry(pg_conn, uid, 2, "PLANNING", anilist_updated_at=now - timedelta(days=10))

    resp = client.get("/queue", params={"status": "PLANNING"})
    assert resp.status_code == 200
    pos_alpha = resp.text.index('data-anime-id="2"')
    pos_zeta = resp.text.index('data-anime-id="1"')
    assert pos_alpha < pos_zeta, "PLANNING tab must stay title-ordered, not recency-ordered"


# ── Drag/back-link guards for the WATCHING tab specifically ────────────────────

def test_watching_card_not_draggable_and_has_no_drag_handle(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _user_id(pg_conn)
    _insert_anime(pg_conn, 1, "Watching Show")
    _insert_entry(pg_conn, uid, 1, "WATCHING")

    resp = client.get("/queue?status=WATCHING")
    card = _item_html(resp.text, 1)
    assert 'draggable="false"' in card
    assert "queue-drag-handle" not in card


def test_planning_card_still_draggable_with_drag_handle(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _user_id(pg_conn)
    _insert_anime(pg_conn, 1, "Planning Show")
    _insert_entry(pg_conn, uid, 1, "PLANNING")

    resp = client.get("/queue?status=PLANNING")
    card = _item_html(resp.text, 1)
    assert 'draggable="true"' in card
    assert "queue-drag-handle" in card


def test_watching_card_notes_back_targets_watching_tab(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _user_id(pg_conn)
    _insert_anime(pg_conn, 1, "Watching Show")
    _insert_entry(pg_conn, uid, 1, "WATCHING")

    resp = client.get("/queue?status=WATCHING")
    card = _item_html(resp.text, 1)
    assert 'data-notes-back="WATCHING"' in card


def test_planning_card_notes_back_unchanged(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _user_id(pg_conn)
    _insert_anime(pg_conn, 1, "Planning Show")
    _insert_entry(pg_conn, uid, 1, "PLANNING")

    resp = client.get("/queue?status=PLANNING")
    card = _item_html(resp.text, 1)
    assert 'data-notes-back="PLANNING"' in card
