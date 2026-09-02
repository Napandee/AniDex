"""
Coverage for issue #221 — full-text search over personal notes
(personal_notes.notes, rewatch_notes.note, episode_notes.note), folded into the
existing GET /search endpoint rather than a separate search box.

Verified against a real Postgres, same pattern as tests/test_collections.py and
tests/test_streaming_coverage.py: a real FastAPI TestClient driving the actual
GET /search route against the real schema.sql shape. Plain ILIKE, no new
migration — per the issue's own scope note, this app's realistic per-user note
volume doesn't justify a tsvector/GIN index, and library_entries/personal_notes
are both already unique per (user_id, anime_id) so the query can't fan out
duplicate rows.

Covers the acceptance criteria from issue #221:
  1. A user can search their own personal notes text and get matching anime back
     (personal_notes.notes, rewatch_notes.note, episode_notes.note all covered).
  2. Search is scoped per-user; no cross-user leakage.
  3. No regression to the existing title-only search.
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
        cur.execute("DELETE FROM episode_notes")
        cur.execute("DELETE FROM rewatch_notes")
        cur.execute("DELETE FROM personal_notes")
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM invites")
        cur.execute("DELETE FROM users")


def _register_and_login(client, email="owner@example.com", password="correct horse battery staple"):
    """First account on an empty `users` table bootstraps as admin with no invite
    needed (see CLAUDE.md's invite-only signup) — `_clean_tables` above leaves
    `users` empty at the start of every test so this always succeeds."""
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def _invite(pg_conn, email):
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO invites (email) VALUES (%s)", (email,))


def _current_user_id(pg_conn, email):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        return cur.fetchone()[0]


def _insert_anime(pg_conn, anime_id, title):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (%s, %s)",
            (anime_id, title),
        )


def _insert_entry(pg_conn, user_id, anime_id, status="WATCHING"):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status) VALUES (%s, %s, %s)",
            (user_id, anime_id, status),
        )


# ── 1. A user can search their own notes text and get matching anime back ──────

def test_search_matches_personal_notes_text(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")

    _insert_anime(pg_conn, 1, "Completely Unrelated Title")
    _insert_entry(pg_conn, uid, 1)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO personal_notes (user_id, anime_id, notes) VALUES (%s, %s, %s)",
            (uid, 1, "Watched this with my partner on a rainy weekend, great comfort show"),
        )

    resp = client.get("/search", params={"q": "rainy weekend"})
    assert resp.status_code == 200
    assert "Completely Unrelated Title" in resp.text


def test_search_matches_rewatch_notes_text(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")

    _insert_anime(pg_conn, 2, "Another Show")
    _insert_entry(pg_conn, uid, 2)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rewatch_notes (user_id, anime_id, repeat_count, note) VALUES (%s, %s, %s, %s)",
            (uid, 2, 1, "second time through, noticed the foreshadowing in episode 3"),
        )

    resp = client.get("/search", params={"q": "foreshadowing"})
    assert resp.status_code == 200
    assert "Another Show" in resp.text


def test_search_matches_episode_notes_text(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")

    _insert_anime(pg_conn, 3, "Episodic Show")
    _insert_entry(pg_conn, uid, 3)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episode_notes (user_id, anime_id, episode_number, note) VALUES (%s, %s, %s, %s)",
            (uid, 3, 5, "incredible animation during the fight scene"),
        )

    resp = client.get("/search", params={"q": "fight scene"})
    assert resp.status_code == 200
    assert "Episodic Show" in resp.text


def test_search_still_matches_titles_no_regression(pg_conn, app_module, client):
    """No regression to the existing title-only search."""
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")

    _insert_anime(pg_conn, 4, "Attack on Something")
    _insert_entry(pg_conn, uid, 4)

    resp = client.get("/search", params={"q": "Attack on"})
    assert resp.status_code == 200
    assert "Attack on Something" in resp.text


def test_search_notes_only_match_flags_matched_notes_badge(pg_conn, app_module, client):
    """A row that matched only via notes (not the title) should render the
    'matched in notes' hint so the user understands why it showed up."""
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")

    _insert_anime(pg_conn, 5, "No Overlap Title")
    _insert_entry(pg_conn, uid, 5)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO personal_notes (user_id, anime_id, notes) VALUES (%s, %s, %s)",
            (uid, 5, "banana pancakes reference"),
        )

    resp = client.get("/search", params={"q": "banana pancakes"})
    assert resp.status_code == 200
    assert "No Overlap Title" in resp.text
    assert "search-matched-notes" in resp.text


def test_search_no_badge_when_title_itself_matches(pg_conn, app_module, client):
    """A straightforward title match shouldn't get the notes-match hint even if
    the anime also happens to have unrelated notes."""
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")

    _insert_anime(pg_conn, 6, "Findable By Title")
    _insert_entry(pg_conn, uid, 6)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO personal_notes (user_id, anime_id, notes) VALUES (%s, %s, %s)",
            (uid, 6, "some unrelated note text"),
        )

    resp = client.get("/search", params={"q": "Findable By Title"})
    assert resp.status_code == 200
    assert "search-matched-notes" not in resp.text


# ── 2. Search is scoped per-user; no cross-user leakage ─────────────────────────

def test_search_notes_never_leak_across_users(pg_conn, app_module, client):
    _register_and_login(client, email="alice@example.com")
    alice_id = _current_user_id(pg_conn, "alice@example.com")

    _invite(pg_conn, "bob@example.com")
    bob_client_resp = client.post(
        "/auth/register",
        data={"email": "bob@example.com", "password": "another strong password"},
        follow_redirects=False,
    )
    assert bob_client_resp.status_code == 303
    bob_id = _current_user_id(pg_conn, "bob@example.com")

    _insert_anime(pg_conn, 7, "Shared Catalog Anime")
    _insert_entry(pg_conn, alice_id, 7)
    _insert_entry(pg_conn, bob_id, 7)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO personal_notes (user_id, anime_id, notes) VALUES (%s, %s, %s)",
            (bob_id, 7, "bob's secret embarrassing confession about this show"),
        )

    # `client` is still logged in as Bob after the second register call (the
    # registration flow logs the new account straight in) — searching as Bob
    # for his own note text should surface the anime.
    resp_bob = client.get("/search", params={"q": "embarrassing confession"})
    assert resp_bob.status_code == 200
    assert "Shared Catalog Anime" in resp_bob.text

    # Log back in as Alice and confirm Bob's note text is invisible to her,
    # even though they share the same underlying anime row.
    client.post("/auth/logout", follow_redirects=False)
    login_resp = client.post(
        "/auth/login",
        data={"email": "alice@example.com", "password": "correct horse battery staple"},
        follow_redirects=False,
    )
    assert login_resp.status_code == 303

    resp_alice = client.get("/search", params={"q": "embarrassing confession"})
    assert resp_alice.status_code == 200
    assert "Shared Catalog Anime" not in resp_alice.text


# ── Issue #478 — zero-result search links into the AniList quick-add flow ──────

def test_search_zero_results_shows_anilist_quick_add_link(pg_conn, app_module, client):
    _register_and_login(client)
    # Empty users table means no library entries exist at all — /search for
    # any query returns zero rows.
    resp = client.get("/search", params={"q": "Some Totally Untracked Show"})
    assert resp.status_code == 200
    assert "Some Totally Untracked Show" in resp.text
    # The CTA must link to library's add-anime flow, pre-filled with the
    # original query via ?addSearch= (Jinja's urlencode uses %20, not +), not
    # a dead end.
    assert 'href="/?addSearch=Some%20Totally%20Untracked%20Show"' in resp.text


def test_search_with_results_has_no_quick_add_link(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _current_user_id(pg_conn, "owner@example.com")
    _insert_anime(pg_conn, 1, "A Real Match")
    _insert_entry(pg_conn, uid, 1)

    resp = client.get("/search", params={"q": "Real Match"})
    assert resp.status_code == 200
    assert "A Real Match" in resp.text
    # A search that DOES return library matches must be unaffected — no
    # quick-add CTA rendered alongside real results.
    assert "addSearch=" not in resp.text


def test_search_empty_query_has_no_quick_add_link(pg_conn, app_module, client):
    _register_and_login(client)
    resp = client.get("/search")
    assert resp.status_code == 200
    assert "addSearch=" not in resp.text
