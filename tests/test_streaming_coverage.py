"""
Coverage for issue #182 — Streaming Coverage v1: marginal-value scoring by
episodes-remaining, own page.

Verified against a real Postgres, same pattern as tests/test_recommendation_snooze.py
and tests/test_totp_2fa.py: this exercises the actual `_compute_streaming_coverage`
query in app.main against the real schema.sql shape (including the new
`user_streaming_services` table), plus a real FastAPI TestClient driving the actual
HTTP routes for the "services I own" form and the /stats summary card.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no
Postgres running.

Covers the acceptance-criteria scenarios from issue #182:
  1. The marginal-value calculation itself: a user with specific owned/un-owned
     services and known library entries produces the expected ranked unlock counts
     (by episodes-remaining, with title-count as a secondary detail) — including an
     entry with an unknown total episode count (ongoing/unlisted) and an entry
     already covered by an owned service being excluded from every other service's
     marginal total.
  2. Completed/Dropped entries never affect the marginal-value ranking, and instead
     surface in the separate "historical footprint" section.
  3. The "services I own" form (Settings) actually persists, including replacing
     the set with an empty one.
  4. /stats's summary card links to /streaming and reflects the top pick.
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
    """Import app.main lazily (after DATABASE_URL/SESSION_SECRET_KEY are set) so it
    picks up the throwaway Postgres, same pattern as tests/test_totp_2fa.py."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    return m


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    """Leaves `users` empty — registration-based tests below rely on the first
    account on an empty table bootstrapping as admin with no invite needed (see
    CLAUDE.md's invite-only signup). Direct-function tests that need a user row use
    the `_seeded_user` fixture instead of relying on this one to provide it."""
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM user_streaming_services")
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM users")


@pytest.fixture()
def _seeded_user(pg_conn):
    """A single pre-existing user (id=USER_ID) for the direct
    _compute_streaming_coverage() tests, which don't go through the registration
    HTTP flow."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
            "VALUES (%s, 'local', 'test', 'test@example.com')",
            (USER_ID,),
        )


def _insert_anime(pg_conn, anime_id, episodes, sites, title=None):
    links = [{"site": s, "url": f"https://example.com/{anime_id}"} for s in sites]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, episodes, external_links) "
            "VALUES (%s, %s, %s, %s::jsonb)",
            (anime_id, title or f"Anime {anime_id}", episodes, __import__("json").dumps(links)),
        )


def _insert_entry(pg_conn, anime_id, status, progress, user_id=USER_ID):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, anime_id, status, progress),
        )


def _set_owned(pg_conn, services, user_id=USER_ID):
    with pg_conn.cursor() as cur:
        for s in services:
            cur.execute(
                "INSERT INTO user_streaming_services (user_id, service) VALUES (%s, %s)",
                (user_id, s),
            )


# ── 1 & 2. The marginal-value calculation, unknown-episode fallback, already-owned
#           exclusion, and Completed/Dropped exclusion from the ranking ───────────

def test_marginal_value_ranking_and_historical_footprint(pg_conn, app_module, _seeded_user):
    _set_owned(pg_conn, ["Crunchyroll"])

    # A — already covered by an owned service: contributes to already_covered, not
    # to any un-owned service's marginal total.
    _insert_anime(pg_conn, 1, episodes=12, sites=["Crunchyroll"])
    _insert_entry(pg_conn, 1, "WATCHING", progress=2)  # remaining = 10

    # B — un-owned Netflix only.
    _insert_anime(pg_conn, 2, episodes=24, sites=["Netflix"])
    _insert_entry(pg_conn, 2, "PLANNING", progress=0)  # remaining = 24

    # C — un-owned Hulu, unknown total episode count (ongoing/unlisted) — must
    # fall back to "at least 1 episode at stake", not 0 and not an error.
    _insert_anime(pg_conn, 3, episodes=None, sites=["Hulu"])
    _insert_entry(pg_conn, 3, "WATCHING", progress=5)  # remaining = 1 (fallback)

    # D — un-owned Netflix AND Hulu: per-service framing (not set-cover), so this
    # title's remaining counts toward BOTH services' marginal totals independently.
    _insert_anime(pg_conn, 4, episodes=10, sites=["Netflix", "Hulu"])
    _insert_entry(pg_conn, 4, "PLANNING", progress=0)  # remaining = 10

    # E — no recognized streaming site at all (STREAMING_SITES filters it out):
    # counts toward the total backlog, but toward neither already_covered nor any
    # service's marginal total.
    _insert_anime(pg_conn, 5, episodes=8, sites=["SomeRandomWiki"])
    _insert_entry(pg_conn, 5, "WATCHING", progress=3)  # remaining = 5

    # F — Completed, on an un-owned service that also has marginal entries above.
    # Must NOT add to Netflix's marginal total — only to the historical footprint.
    _insert_anime(pg_conn, 6, episodes=12, sites=["Netflix"])
    _insert_entry(pg_conn, 6, "COMPLETED", progress=12)

    # G — Dropped, un-owned Hulu — historical footprint only.
    _insert_anime(pg_conn, 7, episodes=13, sites=["Hulu"])
    _insert_entry(pg_conn, 7, "DROPPED", progress=3)

    # H — Paused: not WATCHING/PLANNING/COMPLETED/DROPPED, must be invisible
    # everywhere (not fetched by the coverage query at all).
    _insert_anime(pg_conn, 8, episodes=5, sites=["Netflix"])
    _insert_entry(pg_conn, 8, "PAUSED", progress=1)

    coverage = app_module._compute_streaming_coverage(USER_ID)

    assert coverage["owned"] == ["Crunchyroll"]
    assert coverage["already_covered_remaining"] == 10
    assert coverage["total_backlog_remaining"] == 10 + 24 + 1 + 10 + 5  # = 50

    ranked = {r["service"]: r for r in coverage["ranked"]}
    assert set(ranked) == {"Netflix", "Hulu"}
    assert ranked["Netflix"]["episodes"] == 24 + 10  # B + D, NOT F (completed)
    assert ranked["Netflix"]["titles"] == 2
    assert ranked["Hulu"]["episodes"] == 1 + 10  # C + D, NOT G (dropped)
    assert ranked["Hulu"]["titles"] == 2

    # Ranked descending by episodes unlocked.
    assert coverage["ranked"][0]["service"] == "Netflix"
    assert coverage["ranked"][1]["service"] == "Hulu"

    # Historical footprint: title counts only, Completed/Dropped, separate from
    # the marginal ranking above (and not weighted by episodes-remaining).
    footprint = {f["service"]: f["titles"] for f in coverage["footprint"]}
    assert footprint == {"Netflix": 1, "Hulu": 1}


def test_fully_covered_backlog_has_empty_ranking(pg_conn, app_module, _seeded_user):
    """Acceptance criterion: when every active-status title is already reachable via
    an owned service, the marginal ranking is empty (nothing left to unlock), not an
    error or a service with a zero/negative value."""
    _set_owned(pg_conn, ["Crunchyroll", "Netflix"])
    _insert_anime(pg_conn, 1, episodes=12, sites=["Crunchyroll"])
    _insert_entry(pg_conn, 1, "WATCHING", progress=6)
    _insert_anime(pg_conn, 2, episodes=24, sites=["Netflix"])
    _insert_entry(pg_conn, 2, "PLANNING", progress=0)

    coverage = app_module._compute_streaming_coverage(USER_ID)

    assert coverage["ranked"] == []
    assert coverage["already_covered_remaining"] == coverage["total_backlog_remaining"]


def test_no_owned_services_still_computes_without_erroring(pg_conn, app_module, _seeded_user):
    _insert_anime(pg_conn, 1, episodes=12, sites=["Netflix"])
    _insert_entry(pg_conn, 1, "PLANNING", progress=0)

    coverage = app_module._compute_streaming_coverage(USER_ID)

    assert coverage["owned"] == []
    assert coverage["ranked"][0]["service"] == "Netflix"
    assert coverage["ranked"][0]["episodes"] == 12


# ── 3. The "services I own" Settings form actually persists ────────────────────

@pytest.fixture()
def client(app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


def _register_and_login(client, email="owner@example.com", password="correct horse battery staple"):
    """Registration itself establishes an authenticated session (auth_register_submit
    calls _set_authenticated_session) — the first account on an empty `users` table
    bootstraps as admin automatically with no invite needed, which _clean_tables
    above relies on by leaving `users` empty at the start of every test."""
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def test_streaming_services_form_persists(pg_conn, app_module, client):
    _register_and_login(client)

    resp = client.post(
        "/settings/streaming-services",
        data={"service": ["Crunchyroll", "Netflix"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "saved=streaming_services" in resp.headers["location"]

    with pg_conn.cursor() as cur:
        cur.execute("SELECT service FROM user_streaming_services ORDER BY service")
        rows = [r[0] for r in cur.fetchall()]
    assert rows == ["Crunchyroll", "Netflix"]

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert 'value="Crunchyroll" checked' in settings_page.text
    assert 'value="Netflix" checked' in settings_page.text
    # A site never selected must not come back checked.
    assert 'value="Hulu" checked' not in settings_page.text


def test_streaming_services_form_replaces_full_set(pg_conn, app_module, client):
    """Submitting again with a different (or empty) selection replaces the whole
    set, rather than merging with what was there before."""
    _register_and_login(client)

    client.post(
        "/settings/streaming-services",
        data={"service": ["Crunchyroll", "Netflix"]},
        follow_redirects=False,
    )
    client.post("/settings/streaming-services", data={}, follow_redirects=False)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT service FROM user_streaming_services")
        rows = cur.fetchall()
    assert rows == []


def test_streaming_services_form_ignores_values_outside_allowlist(pg_conn, app_module, client):
    _register_and_login(client)

    client.post(
        "/settings/streaming-services",
        data={"service": ["Crunchyroll", "Not A Real Streaming Site"]},
        follow_redirects=False,
    )

    with pg_conn.cursor() as cur:
        cur.execute("SELECT service FROM user_streaming_services")
        rows = [r[0] for r in cur.fetchall()]
    assert rows == ["Crunchyroll"]


# ── 4. /stats's summary card ────────────────────────────────────────────────────

def test_stats_card_links_to_streaming_and_shows_top_pick(pg_conn, app_module, client):
    _register_and_login(client, email="stats-user@example.com")

    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = 'stats-user@example.com'")
        (uid,) = cur.fetchone()

    _set_owned(pg_conn, ["Crunchyroll"], user_id=uid)
    _insert_anime(pg_conn, 101, episodes=24, sites=["Netflix"])
    _insert_entry(pg_conn, 101, "PLANNING", progress=0, user_id=uid)

    resp = client.get("/stats")
    assert resp.status_code == 200
    assert 'href="/streaming"' in resp.text
    assert "Netflix" in resp.text


def test_stats_card_prompts_setup_when_no_owned_services(pg_conn, app_module, client):
    _register_and_login(client, email="no-owned@example.com")

    resp = client.get("/stats")
    assert resp.status_code == 200
    assert 'href="/streaming"' in resp.text


# ── /streaming page renders for a logged-in user ────────────────────────────────

def test_streaming_page_renders(pg_conn, app_module, client):
    _register_and_login(client, email="streaming-page@example.com")

    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = 'streaming-page@example.com'")
        (uid,) = cur.fetchone()

    _insert_anime(pg_conn, 201, episodes=12, sites=["Netflix"])
    _insert_entry(pg_conn, 201, "WATCHING", progress=3, user_id=uid)

    resp = client.get("/streaming")
    assert resp.status_code == 200
    assert "Netflix" in resp.text
