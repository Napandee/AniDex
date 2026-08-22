"""
Coverage for issue #287 — alert a user when a title just added to Planning isn't
covered by any streaming service they own (`user_streaming_services`, added by #284).

Verified against a real Postgres, same pattern as
tests/test_streaming_availability_notify.py (issue #229's sibling notification
check): this exercises `app.main._notify_if_planning_uncovered` directly against the
real schema.sql shape, with `app.main.notify` monkeypatched to capture what would
have been sent instead of hitting a real channel.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no Postgres
running.

Covers the acceptance-criteria scenarios from issue #287:
  1. A title available only on a service the user doesn't own notifies, naming that
     service.
  2. A title covered by at least one owned service does not notify.
  3. A title with no known streaming availability at all does not notify (nothing to
     name — issue #229's separate "gained availability" check covers that later).
  4. Multiple non-owned services are all named in the notification body.
  5. A non-streaming external link (e.g. an official site) is never treated as
     availability.
  6. Scoped to the requesting user's own owned-services set — another user owning the
     covering service must not suppress this user's notification.
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

USER_ID = 1
OTHER_USER_ID = 2
ANIME_A = 951


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
        for uid in (USER_ID, OTHER_USER_ID):
            cur.execute(
                "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
                "VALUES (%s, 'local', %s, %s)",
                (uid, f"test{uid}", f"test{uid}@example.com"),
            )
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM user_streaming_services")
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")


@pytest.fixture()
def db_module(pg_conn, monkeypatch):
    """Import app.main lazily (after DATABASE_URL/SESSION_SECRET_KEY are set), same
    pattern as tests/test_streaming_availability_notify.py."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    return m


@pytest.fixture()
def sent(db_module, monkeypatch):
    captured = []
    monkeypatch.setattr(db_module, "notify", lambda user_id, title, body: captured.append((user_id, title, body)))
    return captured


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


# ── Not covered by any owned service ────────────────────────────────────────────

def test_uncovered_title_notifies_naming_the_service(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [{"site": "Crunchyroll", "url": "https://example.com"}], title="Sousou no Frieren")
    _own_service(pg_conn, USER_ID, "Netflix")  # owned, but doesn't carry this title

    db_module._notify_if_planning_uncovered(USER_ID, ANIME_A)

    assert len(sent) == 1
    user_id, title, body = sent[0]
    assert user_id == USER_ID
    assert "Sousou no Frieren" in body
    assert "Crunchyroll" in body


def test_uncovered_title_with_no_owned_services_at_all_notifies(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [{"site": "Hulu", "url": "https://example.com"}])

    db_module._notify_if_planning_uncovered(USER_ID, ANIME_A)

    assert len(sent) == 1


def test_multiple_non_owned_services_all_named(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [
        {"site": "Crunchyroll", "url": "https://example.com/cr"},
        {"site": "Hulu", "url": "https://example.com/hulu"},
    ])

    db_module._notify_if_planning_uncovered(USER_ID, ANIME_A)

    assert len(sent) == 1
    body = sent[0][2]
    assert "Crunchyroll" in body
    assert "Hulu" in body


# ── Covered by an owned service — no notification ───────────────────────────────

def test_covered_title_does_not_notify(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [{"site": "Crunchyroll", "url": "https://example.com"}])
    _own_service(pg_conn, USER_ID, "Crunchyroll")

    db_module._notify_if_planning_uncovered(USER_ID, ANIME_A)

    assert sent == []


def test_covered_by_one_of_several_owned_services_does_not_notify(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [
        {"site": "Crunchyroll", "url": "https://example.com/cr"},
        {"site": "Hulu", "url": "https://example.com/hulu"},
    ])
    _own_service(pg_conn, USER_ID, "Hulu")

    db_module._notify_if_planning_uncovered(USER_ID, ANIME_A)

    assert sent == []


# ── No known availability anywhere — nothing to name, no notification ───────────

def test_no_availability_at_all_does_not_notify(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [])

    db_module._notify_if_planning_uncovered(USER_ID, ANIME_A)

    assert sent == []


def test_non_streaming_link_does_not_count_as_availability(pg_conn, db_module, sent):
    # An official-site / wiki link (not in STREAMING_SITES) must not be treated as
    # availability — same allowlist every other external_links filter in this app uses.
    _insert_anime(pg_conn, ANIME_A, [{"site": "Official Site", "url": "https://example.com"}])

    db_module._notify_if_planning_uncovered(USER_ID, ANIME_A)

    assert sent == []


# ── Scoping ──────────────────────────────────────────────────────────────────────

def test_another_users_owned_service_does_not_suppress_this_users_notification(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [{"site": "Crunchyroll", "url": "https://example.com"}])
    _own_service(pg_conn, OTHER_USER_ID, "Crunchyroll")  # a different user owns it

    db_module._notify_if_planning_uncovered(USER_ID, ANIME_A)

    assert len(sent) == 1
    assert sent[0][0] == USER_ID
