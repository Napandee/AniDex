"""
Coverage for issue #236 — a lightweight, sequential-reveal presentation of the
yearly wrap-up (Letterboxd Year-in-Review style pacing) that auto-plays the
first time a user views a given year's wrap-up, then settles into the existing
static grid, with a "Show reveal" button to replay it on demand afterward.

Presentation-only, per the issue's explicit scope: no new backend data (reuses
whatever #wrapup-card/#193/#196 already assemble server-side), and "has this
user seen this year's reveal" is tracked client-side only (localStorage, see
script.js's AniDexWrapupReveal module) — there's deliberately no schema change
and nothing server-side records reveal-seen state.

What's actually testable from pytest (server-side contract only — the actual
auto-play/replay sequencing is client-side JS, same "nothing for pytest to
exercise there" note tests/test_stats_drilldown.py and tests/test_collections.py
make about their own client-side replay mechanics; verified live instead, see
the PR description):

  1. /stats renders #wrapup-card with a "Show reveal" replay button
     (id="wrapup-replay", server-rendered hidden — script.js reveals it once
     data loads) and the `wrapup-reveal-step` marker class on every element
     the sequential reveal paces through (the 4 wrapup-body stats + the 3
     wrapup-extras details from #193).
  2. /stats/wrapped renders its own reveal container (#wrapped-reveal) keyed
     to the actual year via data-wrapup-reveal-year, its own independently-
     scoped replay button (id="wrapped-replay-btn", data-wrapup-reveal-
     scope="wrapped" — kept separate from #wrapup-card's "card" scope per the
     issue's own reasoning: #196's page is always the current calendar year,
     while #wrapup-card's year picker can revisit any past year), and the
     `wrapup-reveal-step` marker on every element it paces through (the pace
     card + 4 headline stats + 3 details).
  3. Neither page's reveal markup appears when there's no data to reveal
     (#wrapup-card hidden entirely when the user has no library_entries at
     all; /stats/wrapped's empty-state branch when there's nothing completed
     this year) — nothing to auto-play, no dangling button.
"""

import datetime
import os
import sys
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extras import Json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


def _d(s):
    return datetime.date.fromisoformat(s)


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
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM invites")
        cur.execute("DELETE FROM users")


@pytest.fixture()
def client(app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


def _register_and_login(client, email="owner@example.com", password="correct horse battery staple"):
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def _user_id(pg_conn, email="owner@example.com"):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        return cur.fetchone()[0]


def _insert_anime(pg_conn, anime_id, genres=None, duration=24, title=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, title_english, genres, episodes, duration) "
            "VALUES (%s, %s, %s, %s, 12, %s)",
            (anime_id, title or f"Anime {anime_id}", title or f"Anime {anime_id}", Json(genres or []), duration),
        )


def _insert_completed_entry(pg_conn, anime_id, finish_date, progress, score=None, user_id=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress, score, finish_date) "
            "VALUES (%s, %s, 'COMPLETED', %s, %s, %s)",
            (user_id, anime_id, progress, score, finish_date),
        )


# ── 1. /stats's #wrapup-card reveal markup ──────────────────────────────────


def test_wrapup_card_renders_replay_button_and_reveal_steps(pg_conn, app_module, client):
    _register_and_login(client)
    uid = _user_id(pg_conn)
    today = datetime.date.today()
    _insert_anime(pg_conn, 1, genres=["Action"])
    _insert_completed_entry(pg_conn, 1, today, progress=12, score=5, user_id=uid)

    resp = client.get("/stats")
    assert resp.status_code == 200
    assert 'id="wrapup-replay"' in resp.text
    assert 'id="wrapup-replay" hidden' in resp.text  # server-rendered hidden; JS reveals it
    assert "Show reveal" in resp.text  # default-locale label for stats_wrapup_replay_btn

    # 4 wrapup-body stats + 3 wrapup-extras details (#193) = 7 paced step
    # elements, plus 1 literal occurrence in the page's own inline <script>
    # (the `.wrapup-reveal-step` selector loadWrapup queries against) = 8.
    assert resp.text.count("wrapup-reveal-step") == 8


def test_wrapup_card_replay_button_server_rendered_hidden_with_no_completions(pg_conn, app_module, client):
    """No library_entries at all → #wrapup-card itself is still server-rendered
    (initWrapup's own pre-existing empty-years guard hides it client-side once
    the page's JS runs, same as before this issue), and the replay button
    starts out `hidden` in the markup either way — script.js only ever reveals
    it after a real, non-empty year's data has loaded (see loadWrapup calling
    AniDexWrapupReveal.init), so there's nothing to spuriously auto-play
    against on a library with zero completions."""
    _register_and_login(client)

    resp = client.get("/stats")
    assert resp.status_code == 200
    assert 'id="wrapup-card"' in resp.text
    assert 'id="wrapup-replay" hidden' in resp.text


# ── 2. /stats/wrapped's own reveal markup ───────────────────────────────────


def test_wrapped_page_renders_own_reveal_container_and_replay_button(pg_conn, app_module, client):
    _register_and_login(client, email="wrapped-reveal@example.com")
    uid = _user_id(pg_conn, "wrapped-reveal@example.com")
    today = datetime.date.today()
    _insert_anime(pg_conn, 1, genres=["Action"])
    _insert_completed_entry(pg_conn, 1, today, progress=12, score=5, user_id=uid)

    resp = client.get("/stats/wrapped")
    assert resp.status_code == 200

    assert 'id="wrapped-replay-btn"' in resp.text
    assert "Show reveal" in resp.text

    assert f'data-wrapup-reveal-year="{today.year}"' in resp.text
    assert 'data-wrapup-reveal-scope="wrapped"' in resp.text
    assert 'data-wrapup-reveal-replay-btn="wrapped-replay-btn"' in resp.text

    # Pace card + 4 headline stats + 3 details (highest-rated, binge, score
    # shift) = 8 paced steps.
    assert resp.text.count("wrapup-reveal-step") == 8


def test_wrapped_page_empty_state_has_no_reveal_markup(pg_conn, app_module, client):
    _register_and_login(client, email="wrapped-empty@example.com")

    resp = client.get("/stats/wrapped")
    assert resp.status_code == 200
    assert 'id="wrapped-replay-btn"' not in resp.text
    assert "data-wrapup-reveal-year" not in resp.text


# ── 3. The two reveal scopes stay independently keyed ───────────────────────


def test_wrapup_card_and_wrapped_page_use_different_reveal_scopes(pg_conn, app_module, client):
    """#wrapup-card's year picker can revisit any past year, while /stats/wrapped
    is always the current calendar year — the two auto-play states must not be
    coupled through a shared localStorage key. Regression guard for the
    scope="card" vs scope="wrapped" split (see script.js's AniDexWrapupReveal)."""
    _register_and_login(client, email="dual-scope@example.com")
    uid = _user_id(pg_conn, "dual-scope@example.com")
    today = datetime.date.today()
    _insert_anime(pg_conn, 1, genres=["Action"])
    _insert_completed_entry(pg_conn, 1, today, progress=12, score=5, user_id=uid)

    stats_resp = client.get("/stats")
    wrapped_resp = client.get("/stats/wrapped")

    assert 'data-wrapup-reveal-scope="wrapped"' in wrapped_resp.text
    # #wrapup-card doesn't carry a static data-wrapup-reveal-scope attribute at
    # all (its year/scope is set client-side after the async /api/stats fetch
    # resolves — see stats.html's loadWrapup calling AniDexWrapupReveal.init
    # with scope: 'card'), so it must never collide with "wrapped".
    assert 'data-wrapup-reveal-scope="wrapped"' not in stats_resp.text
