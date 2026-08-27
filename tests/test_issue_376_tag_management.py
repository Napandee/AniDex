"""
Coverage for issue #376 — a dedicated page to rename/merge/delete custom tags
across a user's whole library at once, distinct from #15's bulk-apply-tags
(which only ever adds tags to an explicit set of entries).

Real Postgres + a real FastAPI TestClient driving /tags, /tags/rename,
/tags/delete end to end, same pattern as tests/test_issue_373_franchise_related_status.py.
"""

import os
import sys
from pathlib import Path

import bcrypt
import psycopg2
import psycopg2.extras
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

USER_ID = 900376
ANIME_A = 900377  # tags: ["comfort watch", "Slow Burn"]
ANIME_B = 900378  # tags: ["Slow burn", "background"]  (case-variant of A's "Slow Burn")
ANIME_C = 900379  # tags: ["unrelated"], mood_tags: ["comfort"]  (must never be touched)


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
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-376-key")
    import app.main as m

    return m


@pytest.fixture()
def client(app_module):
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture(autouse=True)
def _seed(pg_conn):
    with pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("DELETE FROM personal_notes WHERE user_id = %s", (USER_ID,))
        cur.execute("DELETE FROM anime WHERE id IN (%s, %s, %s)", (ANIME_A, ANIME_B, ANIME_C))
        cur.execute("DELETE FROM users WHERE id = %s", (USER_ID,))

        pw_hash = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
        cur.execute(
            "INSERT INTO users (id, email, password_hash, is_admin, auth_provider, auth_provider_id, created_at) "
            "VALUES (%s, %s, %s, false, 'local', %s, now())",
            (USER_ID, "tags376@example.com", pw_hash, "tags376@example.com"),
        )
        for aid, title in ((ANIME_A, "Anime A"), (ANIME_B, "Anime B"), (ANIME_C, "Anime C")):
            cur.execute("INSERT INTO anime (id, title_romaji) VALUES (%s, %s)", (aid, title))

        cur.execute(
            "INSERT INTO personal_notes (user_id, anime_id, personal_tags) VALUES (%s, %s, %s::jsonb)",
            (USER_ID, ANIME_A, '["comfort watch", "Slow Burn"]'),
        )
        cur.execute(
            "INSERT INTO personal_notes (user_id, anime_id, personal_tags) VALUES (%s, %s, %s::jsonb)",
            (USER_ID, ANIME_B, '["Slow burn", "background"]'),
        )
        cur.execute(
            "INSERT INTO personal_notes (user_id, anime_id, personal_tags, mood_tags) VALUES (%s, %s, %s::jsonb, %s::jsonb)",
            (USER_ID, ANIME_C, '["unrelated"]', '["comfort"]'),
        )
    pg_conn.commit()
    yield


def _login(client):
    resp = client.post(
        "/auth/login", data={"email": "tags376@example.com", "password": "testpass123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def _tags_for(pg_conn, anime_id):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT personal_tags FROM personal_notes WHERE user_id = %s AND anime_id = %s",
            (USER_ID, anime_id),
        )
        return cur.fetchone()[0]


def test_tags_page_lists_distinct_tags_case_insensitively_with_counts(pg_conn, client):
    _login(client)
    resp = client.get("/tags")
    assert resp.status_code == 200
    body = resp.text

    # "Slow Burn" (A) and "Slow burn" (B) are the same tag case-insensitively —
    # must appear as exactly one table ROW, not two — the tag's own display
    # name also gets echoed into a hidden input, a datalist <option>, and the
    # rename form's target list, so a raw whole-page substring count would
    # over-count; count the one place a tag's row identity actually lives.
    assert body.lower().count('class="tags-name">slow burn') == 1
    assert "comfort watch" in body
    assert "background" in body
    assert "unrelated" in body


def test_rename_updates_every_entry_that_had_the_tag(pg_conn, client, app_module):
    _login(client)
    resp = client.post(
        "/tags/rename",
        data={"csrf_token": "x", "old_tag": "comfort watch", "new_tag": "cozy"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    assert "cozy" in _tags_for(pg_conn, ANIME_A)
    assert "comfort watch" not in _tags_for(pg_conn, ANIME_A)
    # untouched entries stay untouched
    assert _tags_for(pg_conn, ANIME_B) == ["Slow burn", "background"]


def test_rename_to_an_existing_tag_merges_and_dedupes(pg_conn, client):
    """A rename where the target tag already exists on the SAME entry (merge
    semantics per #376's own framing) must not leave a duplicate."""
    _login(client)
    # Give ANIME_A both "Slow Burn" and, after this, "background" — merging
    # "background" doesn't apply to A directly; instead verify B's two tags
    # ("Slow burn" and "background") merge correctly when renaming "Slow burn"
    # into "background" — B should end up with exactly one "background" entry,
    # not two.
    resp = client.post(
        "/tags/rename",
        data={"csrf_token": "x", "old_tag": "Slow burn", "new_tag": "background"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    b_tags = _tags_for(pg_conn, ANIME_B)
    assert b_tags.count("background") == 1
    assert "Slow burn" not in b_tags

    # A had "Slow Burn" (case-variant) too — it becomes "background" there as well.
    a_tags = _tags_for(pg_conn, ANIME_A)
    assert "background" in a_tags
    assert "Slow Burn" not in a_tags


def test_delete_removes_tag_everywhere_without_touching_others(pg_conn, client):
    _login(client)
    resp = client.post(
        "/tags/delete", data={"csrf_token": "x", "tag": "Slow Burn"}, follow_redirects=False,
    )
    assert resp.status_code == 303

    a_tags = _tags_for(pg_conn, ANIME_A)
    assert "Slow Burn" not in a_tags
    assert "comfort watch" in a_tags  # sibling tag on the same entry survives

    b_tags = _tags_for(pg_conn, ANIME_B)
    assert "Slow burn" not in b_tags  # case-insensitive match also removed
    assert "background" in b_tags


def test_mood_tags_are_never_touched(pg_conn, client):
    """mood_tags is a separate, closed picklist — renaming/deleting a personal
    tag with the same text as a mood tag must not affect mood_tags at all."""
    _login(client)
    client.post(
        "/tags/rename",
        data={"csrf_token": "x", "old_tag": "unrelated", "new_tag": "renamed-unrelated"},
        follow_redirects=False,
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT mood_tags FROM personal_notes WHERE user_id = %s AND anime_id = %s",
            (USER_ID, ANIME_C),
        )
        mood_tags = cur.fetchone()[0]
    assert mood_tags == ["comfort"]


def test_deleting_a_tag_that_matches_a_mood_tag_text_does_not_touch_mood_tags(pg_conn, client):
    """"comfort" exists as both ANIME_C's mood_tags entry and could coincidentally
    be typed as a personal tag elsewhere — deleting a personal tag named "comfort"
    must never reach into mood_tags, a structurally separate column."""
    _login(client)
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE personal_notes SET personal_tags = '[\"comfort\"]'::jsonb "
            "WHERE user_id = %s AND anime_id = %s",
            (USER_ID, ANIME_A),
        )
    pg_conn.commit()

    client.post("/tags/delete", data={"csrf_token": "x", "tag": "comfort"}, follow_redirects=False)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT mood_tags FROM personal_notes WHERE user_id = %s AND anime_id = %s",
            (USER_ID, ANIME_C),
        )
        assert cur.fetchone()[0] == ["comfort"]
