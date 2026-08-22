"""
Coverage for issue #287 — verifies the actual hook wiring: that a real write to
library_entries.status = 'PLANNING', made through the app's real HTTP routes, is
what triggers `app.main._notify_if_planning_uncovered` (unit-tested on its own in
tests/test_planning_coverage_notify.py). This file is the "does the hook actually
fire from the right place, and only once per real transition" half of the coverage.

Three write paths can move a row's status to PLANNING, all hooked per this issue:
  1. POST /api/anime/{id}/status        — single-card status change (_apply_status_change)
  2. POST /api/anime/bulk-status        — UI bulk-status edit (bulk_set_status)
  3. POST /api/anime/{id}/add           — quick-add (add_anime), default status PLANNING

Real Postgres + real FastAPI TestClient + real HTTP routes, same pattern as
tests/test_streaming_coverage.py (register/login establishes the session; the first
account on an empty `users` table bootstraps as admin, no invite needed).

`app.main.notify` is monkeypatched to capture calls instead of hitting a real
channel — same as every other notification test in this suite. `app.main.httpx.post`
is monkeypatched only for the add_anime tests, which (unlike the other two paths)
make unconditional live AniList calls with no ANILIST_MOCK short-circuit; the single-
card endpoint's own AniList call IS skipped under ANILIST_MOCK (set by conftest.py
for the whole suite), so that one needs no httpx mocking at all.

Needs a reachable Postgres via DATABASE_URL — skipped entirely if one isn't
available, same as every other Postgres-backed suite here.

Covers the acceptance-criteria scenarios from issue #287:
  - Adding a title to Planning that isn't covered by any owned service triggers a
    notification through the existing dispatcher, via all three write paths.
  - A title that IS covered by an owned service triggers no notification.
  - Re-posting an already-PLANNING status (a no-op transition) never re-fires the
    notification — the "was this row already PLANNING" guard each call site adds.
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

ANIME_ID = 961


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
        cur.execute("DELETE FROM status_sync_outbox")
        cur.execute("DELETE FROM user_streaming_services")
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM users")


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


def _user_id(pg_conn, email="owner@example.com"):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        return cur.fetchone()[0]


def _insert_anime(pg_conn, anime_id, external_links, title="Test Anime"):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, external_links) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET external_links = EXCLUDED.external_links",
            (anime_id, title, json.dumps(external_links)),
        )


def _own_service(pg_conn, user_id, service):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_streaming_services (user_id, service) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (user_id, service),
        )


def _insert_entry(pg_conn, user_id, anime_id, status):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, anime_id) DO UPDATE SET status = EXCLUDED.status",
            (user_id, anime_id, status),
        )


@pytest.fixture()
def sent(app_module, monkeypatch):
    captured = []
    monkeypatch.setattr(app_module, "notify", lambda user_id, title, body: captured.append((user_id, title, body)))
    return captured


# ── POST /api/anime/bulk-status — no AniList call at all (local-first/outbox) ────

def test_bulk_status_notifies_when_moved_to_uncovered_planning(pg_conn, app_module, client, sent):
    _register_and_login(client)
    user_id = _user_id(pg_conn)
    _insert_anime(pg_conn, ANIME_ID, [{"site": "Crunchyroll", "url": "https://example.com"}], title="Bulk Uncovered")
    _insert_entry(pg_conn, user_id, ANIME_ID, "WATCHING")

    resp = client.post("/api/anime/bulk-status", json={"status": "PLANNING", "anime_ids": [ANIME_ID]})

    assert resp.status_code == 200
    assert len(sent) == 1
    assert "Bulk Uncovered" in sent[0][2]
    assert "Crunchyroll" in sent[0][2]


def test_bulk_status_no_notify_when_covered(pg_conn, app_module, client, sent):
    _register_and_login(client)
    user_id = _user_id(pg_conn)
    _insert_anime(pg_conn, ANIME_ID, [{"site": "Crunchyroll", "url": "https://example.com"}])
    _own_service(pg_conn, user_id, "Crunchyroll")
    _insert_entry(pg_conn, user_id, ANIME_ID, "WATCHING")

    resp = client.post("/api/anime/bulk-status", json={"status": "PLANNING", "anime_ids": [ANIME_ID]})

    assert resp.status_code == 200
    assert sent == []


def test_bulk_status_no_renotify_when_already_planning(pg_conn, app_module, client, sent):
    _register_and_login(client)
    user_id = _user_id(pg_conn)
    _insert_anime(pg_conn, ANIME_ID, [{"site": "Crunchyroll", "url": "https://example.com"}])
    _insert_entry(pg_conn, user_id, ANIME_ID, "PLANNING")  # already Planning

    resp = client.post("/api/anime/bulk-status", json={"status": "PLANNING", "anime_ids": [ANIME_ID]})

    assert resp.status_code == 200
    assert sent == []  # not a genuine transition, must not re-fire


def test_bulk_status_no_notify_for_non_planning_target_status(pg_conn, app_module, client, sent):
    _register_and_login(client)
    user_id = _user_id(pg_conn)
    _insert_anime(pg_conn, ANIME_ID, [{"site": "Crunchyroll", "url": "https://example.com"}])
    _insert_entry(pg_conn, user_id, ANIME_ID, "PLANNING")

    resp = client.post("/api/anime/bulk-status", json={"status": "COMPLETED", "anime_ids": [ANIME_ID]})

    assert resp.status_code == 200
    assert sent == []


# ── POST /api/anime/{id}/status — single-card; skips its AniList call under
#    ANILIST_MOCK (set globally by tests/conftest.py) ────────────────────────────

def test_single_status_endpoint_notifies_on_transition_to_planning(pg_conn, app_module, client, sent):
    _register_and_login(client)
    user_id = _user_id(pg_conn)
    _insert_anime(pg_conn, ANIME_ID, [{"site": "Hulu", "url": "https://example.com"}], title="Single Card Uncovered")
    _insert_entry(pg_conn, user_id, ANIME_ID, "WATCHING")

    resp = client.post(f"/api/anime/{ANIME_ID}/status", json={"status": "PLANNING"})

    assert resp.status_code == 200, resp.text
    assert len(sent) == 1
    assert "Single Card Uncovered" in sent[0][2]
    assert "Hulu" in sent[0][2]


def test_single_status_endpoint_no_renotify_when_already_planning(pg_conn, app_module, client, sent):
    _register_and_login(client)
    user_id = _user_id(pg_conn)
    _insert_anime(pg_conn, ANIME_ID, [{"site": "Hulu", "url": "https://example.com"}])
    _insert_entry(pg_conn, user_id, ANIME_ID, "PLANNING")

    resp = client.post(f"/api/anime/{ANIME_ID}/status", json={"status": "PLANNING"})

    assert resp.status_code == 200
    assert sent == []


# ── POST /api/anime/{id}/add — quick-add; always makes a real AniList call, so the
#    media-fetch + SaveMediaListEntry calls are faked here ─────────────────────────

@pytest.fixture()
def fake_anilist(app_module, monkeypatch):
    """Fakes both httpx.post calls add_anime makes: the media-fetch query and the
    SaveMediaListEntry mutation — routed by inspecting the GraphQL query text, same
    shape AniList's real API would return, just enough for _upsert_anime_row (which
    only requires media['id'] and media['title']['romaji']) and add_anime's own
    entry_id read to succeed without hitting the network."""

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_post(url, json=None, headers=None, timeout=None):
        query = (json or {}).get("query", "")
        if "SaveMediaListEntry" in query:
            return FakeResp({"data": {"SaveMediaListEntry": {"id": 12345, "status": "PLANNING"}}})
        # media-fetch query
        media_id = (json or {}).get("variables", {}).get("id")
        return FakeResp({
            "data": {
                "Media": {
                    "id": media_id,
                    "title": {"romaji": "Add Anime Test", "english": "Add Anime Test"},
                    "externalLinks": [{"site": "HIDIVE", "url": "https://example.com"}],
                }
            }
        })

    monkeypatch.setattr(app_module.httpx, "post", fake_post)


def test_add_anime_notifies_when_uncovered(pg_conn, app_module, client, sent, fake_anilist):
    _register_and_login(client)
    user_id = _user_id(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (%s, 'anilist_token', 'fake-token')",
            (user_id,),
        )

    resp = client.post(f"/api/anime/{ANIME_ID}/add", json={"status": "PLANNING"})

    assert resp.status_code == 200, resp.text
    assert len(sent) == 1
    assert "Add Anime Test" in sent[0][2]
    assert "HIDIVE" in sent[0][2]


def test_add_anime_no_notify_when_covered(pg_conn, app_module, client, sent, fake_anilist):
    _register_and_login(client)
    user_id = _user_id(pg_conn)
    _own_service(pg_conn, user_id, "HIDIVE")
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (%s, 'anilist_token', 'fake-token')",
            (user_id,),
        )

    resp = client.post(f"/api/anime/{ANIME_ID}/add", json={"status": "PLANNING"})

    assert resp.status_code == 200, resp.text
    assert sent == []
