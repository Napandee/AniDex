"""
Coverage for issue #283 — the signup flow never told a new user that Crunchyroll/
Netflix credentials are optional. `run_full_sync.py` already skips those two steps
cleanly when no credentials are configured (see CLAUDE.md's Architecture section);
the gap was purely messaging, not sync logic. This is a copy-only fix: a new i18n
key (`register_cr_netflix_optional_note`) rendered on `/auth/register`.

Same real-Postgres + real-TestClient pattern as tests/test_issue_268_cred_card_collapse.py,
skipped entirely if no Postgres is reachable.
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
I18N_DIR = Path(__file__).resolve().parent.parent / "app" / "i18n"


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
        cur.execute("DELETE FROM users")


@pytest.fixture()
def client(app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


def test_register_page_renders_cr_netflix_optional_note(client):
    """A brand-new, unauthenticated visitor hitting /auth/register (before any
    account exists) must see copy stating CR/Netflix credentials are optional —
    the exact gap issue #283 filed against."""
    resp = client.get("/auth/register")
    assert resp.status_code == 200

    expected = json.loads((I18N_DIR / "en.json").read_text(encoding="utf-8"))[
        "register_cr_netflix_optional_note"
    ]
    assert expected in resp.text


def test_register_note_mentions_both_providers_as_optional():
    """Guard the copy itself, not just its presence: it must actually name both
    Crunchyroll and Netflix as optional, not just gesture vaguely at 'services'."""
    note = json.loads((I18N_DIR / "en.json").read_text(encoding="utf-8"))[
        "register_cr_netflix_optional_note"
    ]
    assert "Crunchyroll" in note
    assert "Netflix" in note
    assert "optional" in note.lower()


def test_settings_credentials_help_still_states_cr_netflix_are_optional(client):
    """Regression guard for the Settings-side half of the same messaging gap —
    `sync_credentials_help` already covered this before #283, don't let a future
    edit silently drop it."""
    resp = client.post(
        "/auth/register",
        data={"email": "owner@example.com", "password": "correct horse battery staple"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get("/settings")
    assert resp.status_code == 200

    expected = json.loads((I18N_DIR / "en.json").read_text(encoding="utf-8"))[
        "sync_credentials_help"
    ]
    assert expected in resp.text
