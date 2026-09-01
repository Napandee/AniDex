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

Issue #444/#452 replaced /stats/wrapped's own use of this reveal with a
full-screen animated slide flow (AniDexWrappedFlow) — #wrapup-card on /stats
is untouched and still uses AniDexWrapupReveal exactly as before. Section 2
below covers the new flow's server-side contract instead of the old one.

What's actually testable from pytest (server-side contract only — the actual
auto-play/replay/slide sequencing is client-side JS, same "nothing for pytest
to exercise there" note tests/test_stats_drilldown.py and
tests/test_collections.py make about their own client-side replay mechanics;
verified live instead, see the PR description):

  1. /stats renders #wrapup-card with a "Show reveal" replay button
     (id="wrapup-replay", server-rendered hidden — script.js reveals it once
     data loads) and the `wrapup-reveal-step` marker class on every element
     the sequential reveal paces through (the 4 wrapup-body stats + the 3
     wrapup-extras details from #193).
  2. /stats/wrapped renders the new slide-flow's server-side contract: a
     "Play recap" trigger (id="wrapped-play-btn"), the inlined
     window.WRAPPED_DATA payload AniDexWrappedFlow builds its slides from,
     and the empty #wrapped-stage shell keyed to the actual year via
     data-wrapped-year — no `wrapup-reveal-step`/`data-wrapup-reveal-*`
     markup at all anymore (that belonged to the old in-place stagger this
     replaced; #wrapup-card's own use of those on /stats is untouched).
  3. Neither page's reveal/flow markup appears when there's no data to show
     (#wrapup-card hidden entirely when the user has no library_entries at
     all; /stats/wrapped's empty-state branch when there's nothing completed
     this year) — nothing to auto-play, no dangling button, no WRAPPED_DATA.
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


# ── 2. /stats/wrapped's slide-flow markup (issue #444/#452) ─────────────────


def test_wrapped_page_renders_play_button_and_stage_shell(pg_conn, app_module, client):
    _register_and_login(client, email="wrapped-reveal@example.com")
    uid = _user_id(pg_conn, "wrapped-reveal@example.com")
    today = datetime.date.today()
    _insert_anime(pg_conn, 1, genres=["Action"])
    _insert_completed_entry(pg_conn, 1, today, progress=12, score=5, user_id=uid)

    resp = client.get("/stats/wrapped")
    assert resp.status_code == 200

    assert 'id="wrapped-play-btn"' in resp.text
    assert "Play recap" in resp.text

    assert "window.WRAPPED_DATA = " in resp.text
    assert '"has_data": true' in resp.text  # real completion above → non-empty payload

    assert f'data-wrapped-year="{today.year}"' in resp.text
    assert 'id="wrapped-stage"' in resp.text
    assert 'id="wrapped-stage-slides"' in resp.text

    # The old in-place stagger's markup this flow replaced must be fully gone
    # from this page — #wrapup-card on /stats still uses it untouched.
    assert "wrapup-reveal-step" not in resp.text
    assert "data-wrapup-reveal-scope" not in resp.text
    assert 'id="wrapped-replay-btn"' not in resp.text


def test_wrapped_page_empty_state_has_no_stage_markup(pg_conn, app_module, client):
    _register_and_login(client, email="wrapped-empty@example.com")

    resp = client.get("/stats/wrapped")
    assert resp.status_code == 200
    assert 'id="wrapped-play-btn"' not in resp.text
    assert 'id="wrapped-stage"' not in resp.text
    assert "window.WRAPPED_DATA" not in resp.text


# ── 3. The two reveal/flow mechanisms stay independent ──────────────────────


def test_wrapup_card_and_wrapped_page_use_different_mechanisms(pg_conn, app_module, client):
    """#wrapup-card's year picker can revisit any past year and still uses the
    old in-place AniDexWrapupReveal stagger (scope 'card'); /stats/wrapped is
    always the current calendar year and now uses the full-screen
    AniDexWrappedFlow instead (issue #444/#452) — the two must not leak each
    other's markup."""
    _register_and_login(client, email="dual-scope@example.com")
    uid = _user_id(pg_conn, "dual-scope@example.com")
    today = datetime.date.today()
    _insert_anime(pg_conn, 1, genres=["Action"])
    _insert_completed_entry(pg_conn, 1, today, progress=12, score=5, user_id=uid)

    stats_resp = client.get("/stats")
    wrapped_resp = client.get("/stats/wrapped")

    assert 'id="wrapped-stage"' in wrapped_resp.text
    assert 'id="wrapped-stage"' not in stats_resp.text

    assert 'id="wrapup-replay"' in stats_resp.text
    assert 'id="wrapup-replay"' not in wrapped_resp.text
