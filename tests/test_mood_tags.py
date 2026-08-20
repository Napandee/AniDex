"""
Coverage for issue #218 — mood tags at log time on personal notes, StoryGraph-
inspired. Adds a dedicated `personal_notes.mood_tags` JSONB array column
(migration 026), validated in application code against a fixed picklist
(app/main.py's MOOD_TAGS), same pattern as STREAMING_SITES / the #182
user_streaming_services allowlist.

Same real-Postgres pattern as tests/test_sessions.py: applies the actual
schema.sql (which already includes mood_tags — schema.sql is the fresh-install
target, migrations/ only matter for upgrading an already-running instance) and
drives the real FastAPI routes via TestClient, not internal functions
directly, so this is genuine end-to-end coverage of the notes form, the JSON
API, and the library page's rendering of the new mood pills — not just proof
the Python compiles.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres
pattern .github/workflows/pr-validate.yml provisions) — skipped entirely if
one isn't available, so `pytest tests/` still collects and passes on a
machine with no Postgres running.

Covers the acceptance criteria from #218:
  1. A user can attach one or more mood values to a personal note (form route
     and JSON API route).
  2. Mood is visible wherever personal tags are already shown (library page).
  3. No regression to existing personal_tags behavior (tags saved alongside
     mood in the same request, unaffected).
Plus the picklist decision itself: an unrecognized mood value submitted
through the JSON API (bypassing the UI's checkboxes) is silently dropped, not
written verbatim and not a 500.
"""

import os
import re
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    """A TestClient wired to the same throwaway Postgres as pg_conn, importing
    app.main lazily (after DATABASE_URL/SESSION_SECRET_KEY are set), same
    pattern as tests/test_sessions.py's app_client fixture. Seeds one logged-in
    user (id 1) and one `anime` row (id 100) with a library_entries row so the
    notes routes and the library page both have something real to operate on."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-mood-tags-secret")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    import app.main as m
    from fastapi.testclient import TestClient

    with pg_conn.cursor() as cur:
        cur.execute(
            "TRUNCATE personal_notes, library_entries, anime, sessions, users "
            "RESTART IDENTITY CASCADE"
        )
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
            "VALUES (1, 'local', 'a@example.com', 'a@example.com', %s, true)",
            (m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode(),),
        )
        cur.execute(
            "INSERT INTO anime (id, title_romaji, title_english) "
            "VALUES (100, 'Test Anime', 'Test Anime EN')"
        )
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress) "
            "VALUES (1, 100, 'WATCHING', 3)"
        )

    client = TestClient(m.app)
    resp = client.post(
        "/auth/login", data={"email": "a@example.com", "password": "password123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return client, m


# ── MOOD_TAGS / _filter_mood_tags — pure-function coverage ─────────────────────


def test_filter_mood_tags_keeps_only_allowlisted_values(app_client):
    _, m = app_client
    assert m._filter_mood_tags(["intense", "not_real", "sad"]) == ["intense", "sad"]


def test_filter_mood_tags_dedupes_and_orders_by_mood_tags_order(app_client):
    _, m = app_client
    assert m._filter_mood_tags(["sad", "comfort", "sad"]) == ["comfort", "sad"]


def test_filter_mood_tags_empty_input():
    import app.main as m
    assert m._filter_mood_tags([]) == []
    assert m._filter_mood_tags(None) == []


# ── Notes form route (multipart/form checkboxes) ────────────────────────────


def test_notes_form_page_renders_mood_checkboxes(app_client):
    client, m = app_client
    resp = client.get("/anime/100/notes")
    assert resp.status_code == 200
    for mood in m.MOOD_TAGS:
        assert f'value="{mood}"' in resp.text


def test_posting_notes_form_with_mood_checkboxes_saves_them(app_client):
    client, m = app_client
    resp = client.post(
        "/anime/100/notes",
        data={
            "notes": "Great show",
            "personal_tags": "isekai, weekend watch",
            "mood": ["comfort", "hype"],
            "back": "WATCHING",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    row = m.db.fetchone(
        "SELECT personal_tags, mood_tags FROM personal_notes WHERE user_id = 1 AND anime_id = 100"
    )
    assert row["personal_tags"] == ["isekai", "weekend watch"]
    assert row["mood_tags"] == ["comfort", "hype"]


def test_notes_form_reflects_previously_saved_mood_as_checked(app_client):
    client, m = app_client
    client.post(
        "/anime/100/notes",
        data={"mood": ["sad"], "back": "WATCHING"},
        follow_redirects=False,
    )
    resp = client.get("/anime/100/notes")
    assert resp.status_code == 200
    assert re.search(r'value="sad"[^>]*checked', resp.text, re.DOTALL)
    # A mood that was never saved must not render as checked.
    assert not re.search(r'value="hype"[^>]*checked', resp.text, re.DOTALL)


def test_saving_notes_without_mood_clears_previously_set_mood(app_client):
    """_upsert_personal_notes is a full-replace, same as personal_tags — an
    unchecked mood checkbox on save must clear it, not leave the old value."""
    client, m = app_client
    client.post("/anime/100/notes", data={"mood": ["intense"], "back": "WATCHING"}, follow_redirects=False)
    row = m.db.fetchone("SELECT mood_tags FROM personal_notes WHERE user_id = 1 AND anime_id = 100")
    assert row["mood_tags"] == ["intense"]

    client.post("/anime/100/notes", data={"back": "WATCHING"}, follow_redirects=False)
    row = m.db.fetchone("SELECT mood_tags FROM personal_notes WHERE user_id = 1 AND anime_id = 100")
    assert row["mood_tags"] == []


# ── JSON API route ───────────────────────────────────────────────────────────


def test_json_api_saves_mood_tags(app_client):
    client, m = app_client
    resp = client.post("/api/anime/100/notes", json={"mood": ["wholesome", "funny"]})
    assert resp.status_code == 200
    row = m.db.fetchone("SELECT mood_tags FROM personal_notes WHERE user_id = 1 AND anime_id = 100")
    assert row["mood_tags"] == ["wholesome", "funny"]


def test_json_api_silently_drops_unrecognized_mood_values(app_client):
    """A payload sent directly to the JSON API (bypassing the UI's fixed
    checkboxes) with a bogus mood value must not crash and must not write the
    bogus value — only real MOOD_TAGS entries survive."""
    client, m = app_client
    resp = client.post("/api/anime/100/notes", json={"mood": ["hype", "made_up_mood"]})
    assert resp.status_code == 200
    row = m.db.fetchone("SELECT mood_tags FROM personal_notes WHERE user_id = 1 AND anime_id = 100")
    assert row["mood_tags"] == ["hype"]


# ── Library page — mood visible wherever personal tags already are ─────────


def test_library_page_renders_mood_pill_alongside_personal_tag(app_client):
    client, m = app_client
    client.post(
        "/anime/100/notes",
        data={"personal_tags": "background watching", "mood": ["dark"], "back": "WATCHING"},
        follow_redirects=False,
    )
    resp = client.get("/?status=WATCHING")
    assert resp.status_code == 200
    assert 'class="mood-tag"' in resp.text
    assert 'class="personal-tag"' in resp.text
    # Rendered label comes from the mood_dark i18n key, not the raw slug.
    en = m.i18n.translator("en")
    assert en("mood_dark") in resp.text
