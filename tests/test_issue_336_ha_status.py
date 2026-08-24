"""
Coverage for issue #336 — a combined, PAT-authenticated `/api/ha/status` endpoint
for polling from Home Assistant's RESTful `sensor:` integration (sync health, queue
length/next-up, episodes airing today/this week — see the route's own docstring in
app/main.py for the "why one combined endpoint" reasoning).

Same real-Postgres pattern as tests/test_mood_tags.py / tests/test_pat_and_mcp_server.py:
applies the actual schema.sql and drives the real FastAPI route via TestClient, not
internal functions directly. Needs a reachable Postgres via DATABASE_URL (the same
throwaway-Postgres pattern .github/workflows/pr-validate.yml provisions) — skipped
entirely if one isn't available.

Covers the acceptance criteria from #336:
  1. Sync-health and airing-today/week data reachable via a single PAT-authenticated
     JSON GET.
  2. A missing/invalid/revoked PAT is rejected with 401 before any data is returned.
  3. Cross-user isolation — one user's PAT never surfaces another user's data.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import pat  # noqa: E402  (needs sys.path insert above)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


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
def app_client(pg_conn, monkeypatch):
    """A TestClient wired to the same throwaway Postgres as pg_conn. Seeds two users
    (id 1, id 2 — for cross-user isolation), a PAT per user, one anime each user has
    in PLANNING (queue), a second anime in WATCHING with an airing_schedule_cache row
    due within the hour (today) and a third airing in 3 days (this week but not
    today), and one completed sync_log row for user 1 only."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-ha-status-secret")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    import app.main as m
    from fastapi.testclient import TestClient

    with pg_conn.cursor() as cur:
        cur.execute(
            "TRUNCATE personal_access_tokens, sync_log, airing_schedule_cache, "
            "library_entries, anime, sessions, users RESTART IDENTITY CASCADE"
        )
        for uid, email in ((1, "a@example.com"), (2, "b@example.com")):
            cur.execute(
                "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
                "VALUES (%s, 'local', %s, %s, %s, true)",
                (uid, email, email, m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode()),
            )
        cur.execute(
            "INSERT INTO anime (id, title_romaji, title_english) VALUES "
            "(100, 'Queue Anime', 'Queue Anime EN'), "
            "(101, 'Airing Soon Anime', NULL), "
            "(102, 'Airing Later Anime', NULL)"
        )
        # user 1: one queued (PLANNING) entry, one watching entry with two upcoming episodes
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress) VALUES "
            "(1, 100, 'PLANNING', 0), (1, 101, 'WATCHING', 3), (1, 102, 'WATCHING', 1)"
        )
        # user 2: their own queued entry, isolated from user 1's data
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress) VALUES (2, 100, 'PLANNING', 0)"
        )
        cur.execute(
            "INSERT INTO airing_schedule_cache (anime_id, episode, airing_at) VALUES "
            "(101, 4, now() + interval '1 hour'), "
            "(102, 2, now() + interval '3 days')"
        )
        cur.execute(
            "INSERT INTO sync_log (user_id, type, status, steps) VALUES "
            "(1, 'full_sync', 'ok', %s)",
            ('[{"service": "anilist_postgres", "status": "ok"}]',),
        )

    _, token1 = pat.create_token(1, "HA sensor")
    _, token2 = pat.create_token(2, "HA sensor")
    revoked_id, revoked_token = pat.create_token(1, "revoked token")
    pat.revoke_token(1, revoked_id)

    client = TestClient(m.app)
    return client, {"user1": token1, "user2": token2, "revoked": revoked_token}


# ── Auth ─────────────────────────────────────────────────────────────────────


def test_missing_bearer_token_is_401(app_client):
    client, _ = app_client
    resp = client.get("/api/ha/status")
    assert resp.status_code == 401


def test_malformed_bearer_token_is_401(app_client):
    client, _ = app_client
    resp = client.get("/api/ha/status", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_revoked_token_is_401(app_client):
    client, tokens = app_client
    resp = client.get("/api/ha/status", headers={"Authorization": f"Bearer {tokens['revoked']}"})
    assert resp.status_code == 401


def test_session_cookie_alone_is_not_accepted(app_client):
    """This endpoint is PAT-only by design (see _require_pat_user's docstring) — a
    logged-in browser session with no bearer token should still be rejected."""
    client, _ = app_client
    login_resp = client.post(
        "/auth/login", data={"email": "a@example.com", "password": "password123"},
        follow_redirects=False,
    )
    assert login_resp.status_code == 303
    resp = client.get("/api/ha/status")
    assert resp.status_code == 401


# ── Payload shape and content ────────────────────────────────────────────────


def test_valid_token_returns_combined_status(app_client):
    client, tokens = app_client
    resp = client.get("/api/ha/status", headers={"Authorization": f"Bearer {tokens['user1']}"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["sync"]["running"] is False
    assert body["sync"]["last_result"] == "ok"
    assert body["sync"]["steps"] == [{"service": "anilist_postgres", "status": "ok"}]

    assert body["queue"]["length"] == 1
    assert body["queue"]["next_up"] == "Queue Anime EN"

    assert body["airing"]["today"] == 1
    assert body["airing"]["this_week"] == 2


def test_cross_user_isolation(app_client):
    """user 2's token must only ever see user 2's own queue/airing/sync data, never
    user 1's, even though both share the same anime catalog rows."""
    client, tokens = app_client
    resp = client.get("/api/ha/status", headers={"Authorization": f"Bearer {tokens['user2']}"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["queue"]["length"] == 1
    assert body["queue"]["next_up"] == "Queue Anime EN"
    # user 2 has no WATCHING entries, so airing_schedule_cache's join yields nothing
    assert body["airing"]["today"] == 0
    assert body["airing"]["this_week"] == 0
    # user 2 has no sync_log rows at all, so no run status/steps to report — but
    # last_synced still comes from library_entries.synced_at's own DEFAULT now()
    # (same as /api/sync/status), which every inserted row gets regardless of
    # whether a real sync job has ever run, so it's populated, not null.
    assert body["sync"]["last_result"] is None
    assert body["sync"]["steps"] == []
    assert body["sync"]["last_synced"] is not None
