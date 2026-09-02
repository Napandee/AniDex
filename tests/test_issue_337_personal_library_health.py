"""
Coverage for issue #337 — "Library health", the personal-scope companion to
#202's admin Data Quality tab, rendered as a small card on Settings > Sync for
every user (not just admins). The underlying detection queries themselves
(`_data_quality_signals(user_id=...)` scoping) are covered in
tests/test_admin_data_quality.py's issue-#337 section; this file covers the
route/template layer — that the Settings page actually renders what that
function returns, and that it never leaks another user's data into the page.

Same real-Postgres TestClient pattern as tests/test_mood_tags.py. Needs a
reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


@pytest.fixture()
def app_client(pg_conn, monkeypatch):
    """Seeds two logged-in-capable users: user 1 (clean library, no issues) and
    user 2 (one orphaned personal_notes row — a note for an anime no longer in
    their library). Returns (client, m, login_fn) so each test logs in as
    whichever user it needs."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-337-secret")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    import app.main as m
    from fastapi.testclient import TestClient

    with pg_conn.cursor() as cur:
        cur.execute(
            "TRUNCATE personal_notes, recommendation_scores, sync_log, library_entries, "
            "anime, sessions, users RESTART IDENTITY CASCADE"
        )
        for uid, email in ((1, "clean@example.com"), (2, "messy@example.com")):
            cur.execute(
                "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_active) "
                "VALUES (%s, 'local', %s, %s, %s, true)",
                (uid, email, email, m.bcrypt.hashpw(b"password123", m.bcrypt.gensalt()).decode()),
            )
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (100, 'Orphaned Note Anime')"
        )
        # user 2 only: a personal_notes row with no matching library_entries row.
        cur.execute(
            "INSERT INTO personal_notes (user_id, anime_id, notes) VALUES (2, 100, 'orphaned')"
        )

    client = TestClient(m.app)

    def login(email):
        resp = client.post(
            "/auth/login", data={"email": email, "password": "password123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    return client, m, login


# base.html embeds the *entire* i18n string map verbatim as `window.I18N` on
# every page (issue #147) — including library_health_all_clear's raw English
# value — so a bare substring check for that sentence is always true whether
# or not the card actually rendered "healthy". The rendered occurrence is
# wrapped in this specific tag; the I18N blob's copy never is.
_RENDERED_ALL_CLEAR = '<p class="status-ok">No issues found in your library.</p>'


def test_clean_library_shows_all_clear(app_client):
    client, _, login = app_client
    login("clean@example.com")
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert _RENDERED_ALL_CLEAR in resp.text


def test_orphaned_note_surfaces_with_title_and_count(app_client):
    client, _, login = app_client
    login("messy@example.com")
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert _RENDERED_ALL_CLEAR not in resp.text
    assert "1 note(s) attached to an anime no longer in your library" in resp.text
    assert "Orphaned Note Anime" in resp.text


def test_one_users_issue_never_shown_to_another_user(app_client):
    """user 1 has no orphaned notes of their own — user 2's must never leak
    into user 1's rendered Settings page."""
    client, _, login = app_client
    login("clean@example.com")
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Orphaned Note Anime" not in resp.text
    assert _RENDERED_ALL_CLEAR in resp.text
