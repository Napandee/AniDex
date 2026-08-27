"""
Coverage for issue #373 — the notes page's "Related titles" section (pre-existing,
sourced from anime.relations, sorted by _RELATION_ORDER) used to show only a
boolean "in your library" pill per related title. A franchise/watch-order view is
supposed to show real per-entry watch status and progress, not just membership —
this closes that specific gap rather than rebuilding the section from scratch.

Real Postgres + a real FastAPI TestClient driving the actual /anime/{id}/notes
route end to end, same pattern as tests/test_issue_358_password_reset_token_hash.py
— notes_form() makes too many distinct queries (anime, notes, rewatch, also-watching,
filler x2, related status/episodes) to reasonably monkeypatch db.fetchall/fetchone
without the fake becoming its own maintenance burden.
"""

import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

MAIN_ID = 900373
PREQUEL_ID = 900374  # in library, WATCHING, has progress + episodes
SEQUEL_ID = 900375  # in library, COMPLETED, no episodes on its own anime row
SIDE_STORY_ID = 900376  # not in the user's library at all


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
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-373-key")
    import app.main as m

    return m


@pytest.fixture()
def client(app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture(scope="module", autouse=True)
def _seed(pg_conn):
    with pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "INSERT INTO users (id, email, password_hash, is_admin, auth_provider, auth_provider_id, created_at) "
            "VALUES (9373, 'franchise@example.com', %s, false, 'local', 'franchise@example.com', now())",
            (__import__("bcrypt").hashpw(b"testpass123", __import__("bcrypt").gensalt()).decode(),),
        )

        relations = [
            {"id": PREQUEL_ID, "title": "Prequel Show", "cover": None, "format": "TV", "relation_type": "PREQUEL"},
            {"id": SEQUEL_ID, "title": "Sequel Show", "cover": None, "format": "TV", "relation_type": "SEQUEL"},
            {"id": SIDE_STORY_ID, "title": "Side Story Show", "cover": None, "format": "OVA", "relation_type": "SIDE_STORY"},
        ]
        cur.execute(
            "INSERT INTO anime (id, title_romaji, episodes, relations) VALUES (%s, %s, %s, %s)",
            (MAIN_ID, "Main Show", 12, json.dumps(relations)),
        )
        cur.execute(
            "INSERT INTO anime (id, title_romaji, episodes) VALUES (%s, %s, %s)",
            (PREQUEL_ID, "Prequel Show", 24),
        )
        cur.execute(
            # Deliberately no `episodes` value — a related title whose own anime row
            # exists but without an episode count (e.g. still airing/unconfirmed),
            # so progress must render as "N ep" not "N/None".
            "INSERT INTO anime (id, title_romaji) VALUES (%s, %s)",
            (SEQUEL_ID, "Sequel Show"),
        )
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (%s, %s)",
            (SIDE_STORY_ID, "Side Story Show"),
        )

        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, synced_at) "
            "VALUES (9373, %s, 'WATCHING', 8, now())",
            (PREQUEL_ID,),
        )
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, synced_at) "
            "VALUES (9373, %s, 'COMPLETED', 5, now())",
            (SEQUEL_ID,),
        )
        # SIDE_STORY_ID: deliberately no library_entries row — not in this user's library.
    pg_conn.commit()


def _login(client):
    resp = client.post(
        "/auth/login", data={"email": "franchise@example.com", "password": "testpass123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_related_title_in_library_shows_real_status_and_progress(pg_conn, client):
    _login(client)

    resp = client.get(f"/anime/{MAIN_ID}/notes")
    assert resp.status_code == 200
    body = resp.text

    # PREQUEL_ID: WATCHING, progress 8, its own anime row has episodes=24 → "8/24"
    assert "Watching" in body
    assert "8/24" in body


def test_related_title_with_progress_but_unknown_total_shows_bare_count(pg_conn, client):
    _login(client)

    resp = client.get(f"/anime/{MAIN_ID}/notes")
    body = resp.text

    # SEQUEL_ID: COMPLETED, progress 5, its own anime row has no episodes value.
    assert "Completed" in body
    assert "5 ep" in body
    assert "5/None" not in body


def test_related_title_not_in_library_shows_no_status_badge(pg_conn, client):
    _login(client)

    resp = client.get(f"/anime/{MAIN_ID}/notes")
    body = resp.text

    marker = "Side Story Show"
    assert marker in body
    idx = body.index(marker)
    # Look at a tight window around this specific related-card, not the whole page —
    # other cards legitimately do carry a badge-status span.
    window = body[max(0, idx - 400):idx + 200]
    assert "badge-status" not in window


def test_old_boolean_in_library_pill_is_gone(pg_conn, client):
    """The old related-in-library CSS class/copy is fully retired now that a real
    status badge replaces it — confirms this wasn't left rendering alongside the
    new badge for the in-library cases."""
    _login(client)

    resp = client.get(f"/anime/{MAIN_ID}/notes")
    body = resp.text

    assert "related-in-library" not in body
