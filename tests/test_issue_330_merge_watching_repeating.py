"""
Coverage for issue #330 — merging the library's separate Watching and Repeating
tabs into one, with a cover ribbon and a pass-count chip marking active rewatches.

Same real-Postgres, real-TestClient pattern as tests/test_mood_tags.py: drives
the actual `/` route end to end (not internal functions directly), so this is
genuine coverage of the merged-tab query, the ribbon/chip render conditions, and
that the other four tabs are unaffected.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no
Postgres running.

Covers the acceptance criteria from #330:
  1. Library page has 5 tabs, not 6.
  2. The Watching tab shows both WATCHING and REPEATING entries together.
  3. REPEATING entries show the cover ribbon.
  4. The pass-count chip shows even for repeat_count = 0 (a first rewatch still
     in progress) — not just a completed one.
  5. Bulk-status-select and the per-card status-select both still offer
     REPEATING as a settable status.
"""

import os
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
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-issue-330-secret")
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
        # A plain first-time watch.
        cur.execute(
            "INSERT INTO anime (id, title_romaji, title_english, episodes) "
            "VALUES (100, 'Youjo Senki II', 'Saga of Tanya the Evil II', 12)"
        )
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, repeat_count) "
            "VALUES (1, 100, 'WATCHING', 5, 0)"
        )
        # A rewatch still on its first pass — repeat_count 0, the exact case the
        # old repeat_count-only ribbon/badge/filter condition missed entirely.
        cur.execute(
            "INSERT INTO anime (id, title_romaji, title_english, episodes) "
            "VALUES (101, 'Alderamin on the Sky', 'Alderamin on the Sky', 13)"
        )
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, repeat_count) "
            "VALUES (1, 101, 'REPEATING', 6, 0)"
        )
        # A rewatch several passes deep.
        cur.execute(
            "INSERT INTO anime (id, title_romaji, title_english, episodes) "
            "VALUES (102, 'The Kingdoms of Ruin', 'The Kingdoms of Ruin', 12)"
        )
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, repeat_count) "
            "VALUES (1, 102, 'REPEATING', 5, 3)"
        )
        # A COMPLETED entry — must never appear on the Watching tab.
        cur.execute(
            "INSERT INTO anime (id, title_romaji, title_english, episodes) "
            "VALUES (103, 'Some Finished Show', 'Some Finished Show', 24)"
        )
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, repeat_count) "
            "VALUES (1, 103, 'COMPLETED', 24, 0)"
        )

    client = TestClient(m.app)
    resp = client.post(
        "/auth/login", data={"email": "a@example.com", "password": "password123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return client, m


def test_library_has_five_tabs_not_six(app_client):
    client, _m = app_client
    resp = client.get("/")
    assert resp.status_code == 200
    # One <a class="tab"> per status — Repeating must not be among them. Scoped
    # to the .status-tabs container specifically: "Repeating" legitimately still
    # appears elsewhere on the page, as an option in the bulk/per-card status
    # dropdowns (see test_*_status_select_still_offers_repeating below).
    tabs_start = resp.text.find('class="status-tabs"')
    tabs_end = resp.text.find("</div>", tabs_start)
    tabs_html = resp.text[tabs_start:tabs_end]
    assert tabs_html.count('class="tab') == 5
    assert "Repeating" not in tabs_html


def test_status_equals_repeating_still_works_directly_and_highlights_watching(app_client):
    """A saved Collection (#200) can carry status: REPEATING as its criteria and
    navigate straight to ?status=REPEATING (see script.js's applyCollection()) —
    that must keep returning only REPEATING entries, and the Watching tab should
    show as active rather than leaving no tab visibly selected."""
    client, _m = app_client
    resp = client.get("/?status=REPEATING")
    assert resp.status_code == 200
    assert "Alderamin on the Sky" in resp.text
    assert "The Kingdoms of Ruin" in resp.text
    assert "Saga of Tanya the Evil II" not in resp.text  # WATCHING-only entry excluded
    assert "Some Finished Show" not in resp.text

    tabs_start = resp.text.find('class="status-tabs"')
    tabs_end = resp.text.find("</div>", tabs_start)
    tabs_html = resp.text[tabs_start:tabs_end]
    assert 'class="tab active"' in tabs_html
    assert tabs_html.count("active") == 1  # exactly one tab highlighted, not zero or two


def test_watching_tab_shows_both_watching_and_repeating_entries(app_client):
    client, _m = app_client
    resp = client.get("/?status=WATCHING")
    assert resp.status_code == 200
    assert "Saga of Tanya the Evil II" in resp.text
    assert "Alderamin on the Sky" in resp.text
    assert "The Kingdoms of Ruin" in resp.text
    # The COMPLETED entry must never leak into the merged Watching tab.
    assert "Some Finished Show" not in resp.text


def test_completed_tab_is_unaffected_by_the_merge(app_client):
    client, _m = app_client
    resp = client.get("/?status=COMPLETED")
    assert resp.status_code == 200
    assert "Some Finished Show" in resp.text
    assert "Alderamin on the Sky" not in resp.text
    assert "Saga of Tanya the Evil II" not in resp.text


def test_repeating_entries_show_the_cover_ribbon(app_client):
    client, _m = app_client
    resp = client.get("/?status=WATCHING")
    assert resp.text.count('class="rewatch-ribbon"') == 2  # Alderamin + Kingdoms of Ruin


def test_watching_entry_shows_no_ribbon(app_client):
    client, _m = app_client
    resp = client.get("/?status=WATCHING")
    # Locate the Tanya card specifically and confirm no ribbon inside its markup.
    idx = resp.text.find("Saga of Tanya the Evil II")
    assert idx != -1
    card_start = resp.text.rfind('<div class="card"', 0, idx)
    card_end = resp.text.find('<div class="card"', idx)
    card_html = resp.text[card_start:card_end if card_end != -1 else idx + 2000]
    assert "rewatch-ribbon" not in card_html


def test_pass_chip_shows_even_for_a_first_rewatch_still_in_progress(app_client):
    """The exact bug this issue fixes the sync side of (#328) and the UI side
    of (#330): repeat_count is still 0, but status REPEATING must still show
    the pass chip, not just entries with a completed rewatch already."""
    client, _m = app_client
    resp = client.get("/?status=WATCHING")
    idx = resp.text.find("Alderamin on the Sky")
    assert idx != -1
    card_start = resp.text.rfind('<div class="card"', 0, idx)
    card_end = resp.text.find('<div class="card"', idx)
    card_html = resp.text[card_start:card_end if card_end != -1 else idx + 2000]
    assert 'class="rewatch-badge"' in card_html
    assert "#1" in card_html  # repeat_count 0 -> pass 1


def test_pass_chip_reflects_repeat_count_for_a_later_pass(app_client):
    client, _m = app_client
    resp = client.get("/?status=WATCHING")
    idx = resp.text.find("The Kingdoms of Ruin")
    assert idx != -1
    card_start = resp.text.rfind('<div class="card"', 0, idx)
    card_end = resp.text.find('<div class="card"', idx)
    card_html = resp.text[card_start:card_end if card_end != -1 else idx + 2000]
    assert 'class="rewatch-badge"' in card_html
    assert "#4" in card_html  # repeat_count 3 -> pass 4


def test_bulk_status_select_still_offers_repeating(app_client):
    client, _m = app_client
    resp = client.get("/?status=WATCHING")
    assert 'id="bulk-status-select"' in resp.text
    select_start = resp.text.find('id="bulk-status-select"')
    select_end = resp.text.find("</select>", select_start)
    assert 'value="REPEATING"' in resp.text[select_start:select_end]


def test_per_card_status_select_still_offers_repeating(app_client):
    client, _m = app_client
    resp = client.get("/?status=WATCHING")
    assert 'class="status-select"' in resp.text
    idx = resp.text.find('class="status-select"')
    end = resp.text.find("</select>", idx)
    assert 'value="REPEATING"' in resp.text[idx:end]
