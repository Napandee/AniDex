"""
Coverage for issue #229 — notify a user when a Planning-list title gains its first
AniList `externalLinks` streaming entry.

Verified against a real Postgres, same pattern as tests/test_streaming_coverage.py and
tests/test_year_so_far_wrapped_page.py: this exercises `app.main._check_streaming_
availability` against the real schema.sql shape (including the new
`planning_availability_state` table from migration 022), with `app.main.notify`
monkeypatched to capture what would have been sent instead of hitting a real channel.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no Postgres
running.

Covers the acceptance-criteria scenarios from issue #229:
  1. A Planning title with zero streaming availability, first seen, records a
     baseline and does not notify.
  2. A Planning title already covered the first time it's ever checked does not
     notify either — that's not a "gained" transition.
  3. Once a false baseline exists, gaining a link fires exactly one notification and
     flips the stored state to true.
  4. No further notification on a later sync once state is already true (no spam for
     titles that already had availability).
  5. A non-streaming external link (e.g. an official site, not in STREAMING_SITES)
     never counts as availability.
  6. Only PLANNING-status entries are considered — WATCHING/COMPLETED/etc. are
     excluded entirely.
  7. Scoped to the requesting user only.
  8. A link disappearing (true -> false) is recorded without notifying, and a later
     re-gain (false -> true again) fires a fresh notification — the diff-on-each-sync
     framing, not a fire-once-ever ledger.
"""

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
ANIME_A = 901
ANIME_B = 902


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
        cur.execute("DELETE FROM planning_availability_state")
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")


@pytest.fixture()
def db_module(pg_conn, monkeypatch):
    """Import app.main lazily (after DATABASE_URL/SESSION_SECRET_KEY are set), same
    pattern as tests/test_streaming_coverage.py and tests/test_year_so_far_wrapped_page.py."""
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
            (anime_id, title, __import__("json").dumps(external_links)),
        )


def _insert_library_entry(pg_conn, user_id, anime_id, status):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, anime_id) DO UPDATE SET status = EXCLUDED.status",
            (user_id, anime_id, status),
        )


def _state(pg_conn, user_id, anime_id):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT had_availability, notified_at FROM planning_availability_state "
            "WHERE user_id = %s AND anime_id = %s",
            (user_id, anime_id),
        )
        return cur.fetchone()


# ── First-ever check: baseline only, never a notification ──────────────────────

def test_first_check_with_no_availability_records_baseline_no_notify(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [])
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "PLANNING")

    db_module._check_streaming_availability(USER_ID)

    assert sent == []
    had_availability, notified_at = _state(pg_conn, USER_ID, ANIME_A)
    assert had_availability is False
    assert notified_at is None


def test_first_check_already_covered_records_baseline_no_notify(pg_conn, db_module, sent):
    # A Planning title that already had streaming availability the very first time
    # this check ever runs must not notify — that's not a "gained" event.
    _insert_anime(pg_conn, ANIME_A, [{"site": "Crunchyroll", "url": "https://example.com"}])
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "PLANNING")

    db_module._check_streaming_availability(USER_ID)

    assert sent == []
    had_availability, _ = _state(pg_conn, USER_ID, ANIME_A)
    assert had_availability is True


# ── The actual gained-availability transition ───────────────────────────────────

def test_gaining_a_link_notifies_exactly_once(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [], title="Sousou no Frieren")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "PLANNING")
    db_module._check_streaming_availability(USER_ID)  # baseline: false
    assert sent == []

    _insert_anime(pg_conn, ANIME_A, [{"site": "Crunchyroll", "url": "https://example.com"}], title="Sousou no Frieren")
    db_module._check_streaming_availability(USER_ID)

    assert len(sent) == 1
    user_id, title, body = sent[0]
    assert user_id == USER_ID
    assert "Sousou no Frieren" in body

    had_availability, notified_at = _state(pg_conn, USER_ID, ANIME_A)
    assert had_availability is True
    assert notified_at is not None


def test_no_repeat_notification_once_already_available(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [{"site": "Crunchyroll", "url": "https://example.com"}])
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "PLANNING")
    db_module._check_streaming_availability(USER_ID)  # baseline: already true, no notify
    assert sent == []

    # A second service link appearing (still non-zero -> non-zero) must not notify —
    # explicitly out of scope per issue #229.
    _insert_anime(pg_conn, ANIME_A, [
        {"site": "Crunchyroll", "url": "https://example.com"},
        {"site": "Netflix", "url": "https://example.com/nf"},
    ])
    db_module._check_streaming_availability(USER_ID)

    assert sent == []


def test_non_streaming_link_does_not_count_as_availability(pg_conn, db_module, sent):
    # An official-site / wiki link (not in STREAMING_SITES) must not be treated as
    # availability — same allowlist every other external_links filter in this app uses.
    _insert_anime(pg_conn, ANIME_A, [{"site": "Official Site", "url": "https://example.com"}])
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "PLANNING")

    db_module._check_streaming_availability(USER_ID)

    assert sent == []
    had_availability, _ = _state(pg_conn, USER_ID, ANIME_A)
    assert had_availability is False


# ── Scoping ──────────────────────────────────────────────────────────────────────

def test_only_planning_status_entries_are_considered(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [])
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "WATCHING")

    db_module._check_streaming_availability(USER_ID)

    assert sent == []
    assert _state(pg_conn, USER_ID, ANIME_A) is None  # no row created for a non-Planning entry


def test_scoped_to_requesting_user_only(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [])
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "PLANNING")
    _insert_library_entry(pg_conn, OTHER_USER_ID, ANIME_A, "PLANNING")

    db_module._check_streaming_availability(USER_ID)

    assert _state(pg_conn, USER_ID, ANIME_A) is not None
    assert _state(pg_conn, OTHER_USER_ID, ANIME_A) is None  # untouched by USER_ID's run


# ── Losing then regaining availability ──────────────────────────────────────────

def test_losing_availability_is_recorded_without_notifying(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [{"site": "Crunchyroll", "url": "https://example.com"}])
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "PLANNING")
    db_module._check_streaming_availability(USER_ID)  # baseline: true

    _insert_anime(pg_conn, ANIME_A, [])  # link removed
    db_module._check_streaming_availability(USER_ID)

    assert sent == []
    had_availability, _ = _state(pg_conn, USER_ID, ANIME_A)
    assert had_availability is False


def test_regaining_availability_after_loss_notifies_again(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [])
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "PLANNING")
    db_module._check_streaming_availability(USER_ID)  # baseline: false

    _insert_anime(pg_conn, ANIME_A, [{"site": "Crunchyroll", "url": "https://example.com"}])
    db_module._check_streaming_availability(USER_ID)  # gained: notify #1
    assert len(sent) == 1

    _insert_anime(pg_conn, ANIME_A, [])
    db_module._check_streaming_availability(USER_ID)  # lost: no notify
    assert len(sent) == 1

    _insert_anime(pg_conn, ANIME_A, [{"site": "Netflix", "url": "https://example.com/nf"}])
    db_module._check_streaming_availability(USER_ID)  # regained: notify #2, a fresh transition

    assert len(sent) == 2


def test_multiple_planning_titles_handled_independently(pg_conn, db_module, sent):
    _insert_anime(pg_conn, ANIME_A, [], title="Title A")
    _insert_anime(pg_conn, ANIME_B, [{"site": "Crunchyroll", "url": "https://example.com"}], title="Title B")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "PLANNING")
    _insert_library_entry(pg_conn, USER_ID, ANIME_B, "PLANNING")

    db_module._check_streaming_availability(USER_ID)  # both baselines, no notify
    assert sent == []

    _insert_anime(pg_conn, ANIME_A, [{"site": "Hulu", "url": "https://example.com/hulu"}], title="Title A")
    db_module._check_streaming_availability(USER_ID)

    assert len(sent) == 1
    assert "Title A" in sent[0][2]
