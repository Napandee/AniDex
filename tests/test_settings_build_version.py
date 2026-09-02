"""
Coverage for issue #204 — user-facing "currently deployed commit" link on every
user's own Settings page.

Same real-Postgres + real-TestClient pattern as tests/test_streaming_coverage.py:
this exercises the actual `/settings` route in app.main against the real
schema.sql shape, rather than mocking the DB layer.

The key scenario this issue is about is a *non-admin* user seeing the link — the
value was already shown to admins (plain text, admin-only Operability tab) before
this issue; the point of #204 is surfacing the same value, as a real link, to
everyone else. Since the first account on an empty `users` table always
bootstraps as admin (see CLAUDE.md), the test registers a first "owner" account
and then a second account, and asserts against the second (non-admin) one.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no
Postgres running.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM users")


def _register_and_login(client, email, password="correct horse battery staple"):
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def test_settings_shows_no_link_when_git_sha_unset(pg_conn, app_module, client, monkeypatch):
    """Baseline: with no GIT_SHA in the environment (a plain dev checkout, not a
    built image), Settings falls back to the same "unknown" copy the admin tab
    already uses, and no commit link is rendered at all."""
    monkeypatch.delenv("GIT_SHA", raising=False)
    _register_and_login(client, "owner@example.com")

    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "github.com/Napandee/AniDex/commit/" not in resp.text


def test_settings_build_version_link_visible_to_non_admin_user(pg_conn, app_module, client, monkeypatch):
    """The actual acceptance criterion: a *non-admin* user's own /settings page
    shows a real <a href> link to the currently-deployed commit, reusing the same
    GIT_SHA the admin-only Operability tab already surfaces as plain text."""
    monkeypatch.setenv("GIT_SHA", "abc123def4567890fake")

    # First account bootstraps as admin (invite-only signup, empty `users` table) —
    # register a second account so the one under test is a genuine non-admin. Every
    # account after the first needs an invite row (invite-only signup).
    _register_and_login(client, "owner@example.com")
    client.post("/auth/logout")
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO invites (email) VALUES (%s)", ("regular-user@example.com",))
    _register_and_login(client, "regular-user@example.com")

    with pg_conn.cursor() as cur:
        cur.execute("SELECT email, is_admin FROM users WHERE email = %s", ("regular-user@example.com",))
        row = cur.fetchone()
    assert row is not None
    assert row[1] is False, "test setup bug: second registered account must not be admin"

    resp = client.get("/settings")
    assert resp.status_code == 200

    expected_sha = "abc123def456"  # _build_version() truncates to the first 12 chars
    expected_url = f"https://github.com/Napandee/AniDex/commit/{expected_sha}"

    assert f'href="{expected_url}"' in resp.text, (
        "expected a real <a href> link to the GitHub commit page in a non-admin "
        "user's own /settings response"
    )
    assert expected_sha in resp.text
