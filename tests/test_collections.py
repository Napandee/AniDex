"""
Coverage for issue #200 — Collections: named saved filter combinations over the
library view's existing tag/status/score/format/season/rewatch/sort controls.

Verified against a real Postgres, same pattern as tests/test_streaming_coverage.py:
a real FastAPI TestClient driving the actual /api/collections HTTP routes against
the real schema.sql shape (including the new `collections` table).

"Applying" a saved collection is a client-side replay (script.js clicks/dispatches
events on the real filter controls) — nothing for pytest to exercise there. What
these tests actually verify is the backend contract that replay depends on: saving
the current filter state round-trips exactly, the library page embeds each user's
own saved collections (and only their own) as `window.COLLECTIONS`, and
rename/delete/scoping all behave correctly.

Covers the acceptance-criteria scenarios from issue #200:
  1. Saving the current filter combination persists it correctly (round-trips
     through GET /api/collections and through the library page's embedded JSON).
  2. Rename and delete both work, scoped to the owning user.
  3. A collection stores filter criteria only — unknown keys are dropped, no
     anime ids are ever accepted into `filters`.
  4. Per-user scoping: one user can never see, rename, or delete another user's
     collection.
"""

import json
import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM collections")
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
    """A second (and later) account needs an invite (invite-only signup) — seed
    one directly rather than going through the admin-invite HTTP flow, which is
    covered by its own test elsewhere."""
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO invites (email) VALUES (%s)", (email,))


# ── 1. Saving the current filter state persists correctly ──────────────────────

def test_create_collection_persists_and_returns_it(pg_conn, app_module, client):
    _register_and_login(client)

    filters = {"status": "WATCHING", "format": "TV", "tag": "isekai", "score": "4", "sort": "title"}
    resp = client.post("/api/collections", json={"name": "Cozy TV", "filters": filters})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["collection"]["name"] == "Cozy TV"
    assert data["collection"]["filters"] == filters

    with pg_conn.cursor() as cur:
        cur.execute("SELECT name, filters FROM collections")
        row = cur.fetchone()
    assert row[0] == "Cozy TV"
    assert row[1] == filters


def test_library_page_embeds_only_this_users_collections(pg_conn, app_module, client):
    _register_and_login(client, email="a@example.com")
    client.post("/api/collections", json={"name": "Mine", "filters": {"status": "WATCHING"}})

    page = client.get("/")
    assert page.status_code == 200
    assert '"name": "Mine"' in page.text or '"name":"Mine"' in page.text or "Mine" in page.text
    # The embedded payload is real JSON assigned to window.COLLECTIONS, not just a
    # substring match — parse it out and check shape precisely.
    marker = "window.COLLECTIONS = "
    start = page.text.index(marker) + len(marker)
    end = page.text.index(";", start)
    embedded = json.loads(page.text[start:end])
    assert len(embedded) == 1
    assert embedded[0]["name"] == "Mine"
    assert embedded[0]["filters"] == {"status": "WATCHING"}


def test_create_requires_nonempty_name(pg_conn, app_module, client):
    _register_and_login(client)
    resp = client.post("/api/collections", json={"name": "   ", "filters": {}})
    assert resp.status_code == 400


def test_create_rejects_duplicate_name_for_same_user(pg_conn, app_module, client):
    _register_and_login(client)
    client.post("/api/collections", json={"name": "Dup", "filters": {}})
    resp = client.post("/api/collections", json={"name": "Dup", "filters": {}})
    assert resp.status_code == 409


def test_filters_are_whitelisted_no_per_anime_data_accepted(pg_conn, app_module, client):
    """Scope guardrail: a collection stores filter criteria only. An unknown key
    (e.g. an attempt to smuggle anime ids in) must be silently dropped, not stored."""
    _register_and_login(client)
    resp = client.post(
        "/api/collections",
        json={
            "name": "Whitelisted",
            "filters": {
                "status": "PLANNING",
                "score": "5",
                "anime_ids": [1, 2, 3],
                "personal_tags": ["should not persist"],
            },
        },
    )
    assert resp.status_code == 200
    stored = resp.json()["collection"]["filters"]
    assert stored == {"status": "PLANNING", "score": "5"}
    assert "anime_ids" not in stored
    assert "personal_tags" not in stored


# ── 2. Rename and delete ────────────────────────────────────────────────────────

def test_rename_collection(pg_conn, app_module, client):
    _register_and_login(client)
    created = client.post("/api/collections", json={"name": "Old Name", "filters": {}}).json()["collection"]

    resp = client.patch(f"/api/collections/{created['id']}", json={"name": "New Name"})
    assert resp.status_code == 200
    assert resp.json()["collection"]["name"] == "New Name"

    with pg_conn.cursor() as cur:
        cur.execute("SELECT name FROM collections WHERE id = %s", (created["id"],))
        assert cur.fetchone()[0] == "New Name"


def test_rename_to_existing_name_conflicts(pg_conn, app_module, client):
    _register_and_login(client)
    client.post("/api/collections", json={"name": "Taken", "filters": {}})
    second = client.post("/api/collections", json={"name": "Free", "filters": {}}).json()["collection"]

    resp = client.patch(f"/api/collections/{second['id']}", json={"name": "Taken"})
    assert resp.status_code == 409


def test_update_filters_only_leaves_name_untouched(pg_conn, app_module, client):
    _register_and_login(client)
    created = client.post(
        "/api/collections", json={"name": "Keep Name", "filters": {"status": "WATCHING"}}
    ).json()["collection"]

    resp = client.patch(f"/api/collections/{created['id']}", json={"filters": {"status": "COMPLETED"}})
    assert resp.status_code == 200
    body = resp.json()["collection"]
    assert body["name"] == "Keep Name"
    assert body["filters"] == {"status": "COMPLETED"}


def test_delete_collection(pg_conn, app_module, client):
    _register_and_login(client)
    created = client.post("/api/collections", json={"name": "Gone Soon", "filters": {}}).json()["collection"]

    resp = client.delete(f"/api/collections/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM collections WHERE id = %s", (created["id"],))
        assert cur.fetchone()[0] == 0


def test_delete_nonexistent_collection_404s(pg_conn, app_module, client):
    _register_and_login(client)
    resp = client.delete("/api/collections/999999")
    assert resp.status_code == 404


# ── 3. Per-user scoping ──────────────────────────────────────────────────────────

def test_user_cannot_see_another_users_collections(pg_conn, app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as client_a:
        _register_and_login(client_a, email="user-a@example.com")
        client_a.post("/api/collections", json={"name": "A's collection", "filters": {}})

    _invite(pg_conn, "user-b@example.com")
    with TestClient(app_module.app) as client_b:
        _register_and_login(client_b, email="user-b@example.com")
        resp = client_b.get("/api/collections")
        assert resp.status_code == 200
        assert resp.json()["items"] == []


def test_user_cannot_rename_another_users_collection(pg_conn, app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as client_a:
        _register_and_login(client_a, email="user-a2@example.com")
        created = client_a.post(
            "/api/collections", json={"name": "A's collection", "filters": {}}
        ).json()["collection"]

    _invite(pg_conn, "user-b2@example.com")
    with TestClient(app_module.app) as client_b:
        _register_and_login(client_b, email="user-b2@example.com")
        resp = client_b.patch(f"/api/collections/{created['id']}", json={"name": "Hijacked"})
        assert resp.status_code == 404

    with pg_conn.cursor() as cur:
        cur.execute("SELECT name FROM collections WHERE id = %s", (created["id"],))
        assert cur.fetchone()[0] == "A's collection"


def test_user_cannot_delete_another_users_collection(pg_conn, app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as client_a:
        _register_and_login(client_a, email="user-a3@example.com")
        created = client_a.post(
            "/api/collections", json={"name": "A's collection", "filters": {}}
        ).json()["collection"]

    _invite(pg_conn, "user-b3@example.com")
    with TestClient(app_module.app) as client_b:
        _register_and_login(client_b, email="user-b3@example.com")
        resp = client_b.delete(f"/api/collections/{created['id']}")
        assert resp.status_code == 404

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM collections WHERE id = %s", (created["id"],))
        assert cur.fetchone()[0] == 1


# ── Unauthenticated access ───────────────────────────────────────────────────────

def test_collections_api_requires_auth(pg_conn, app_module, client):
    resp = client.get("/api/collections")
    assert resp.status_code == 401
