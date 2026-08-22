"""
Coverage for issue #235 — the in-app "what's new since your last visit" digest.

Verified against a real Postgres, same pattern as tests/test_streaming_availability_
notify.py and tests/test_totp_2fa.py: the pure content-building function
(_build_whats_new_digest) is exercised directly against the real schema.sql shape
(including the new `digest_last_seen_at` column from migration 028), and the
session-gated wiring (_whats_new_for_request / the _nav_context integration) is
exercised end to end through a real FastAPI TestClient so the "shown at most once per
session, then the watermark advances" behavior is proven against the actual route,
not just asserted about the helper in isolation.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no Postgres
running.

Covers the acceptance-criteria scenarios from issue #235:
  1. A brand-new user (digest_last_seen_at IS NULL) gets no digest on their first
     page view — just a baseline watermark — so existing/new accounts never get their
     entire history dumped into one banner.
  2. New episodes aired since the watermark are included for WATCHING/PLANNING
     library entries (matching _check_airing_episodes' own status filter — the
     hourly job that populates notified_episodes, which this digest reads from
     instead of airing_schedule_cache; see _build_whats_new_digest's docstring for
     why), and excluded for COMPLETED/DROPPED entries or if notified before the
     watermark.
  3. New non-dismissed, non-snoozed recommendations since the watermark are counted
     and the top titles surfaced; dismissed/snoozed/pre-existing ones are excluded.
  4. Sync activity (full_sync/force_full_resync only — not 'recommender', which would
     double-count recommendation content) since the watermark is summarized, including
     surfacing that a run needs attention.
  5. Nothing new at all -> _build_whats_new_digest returns None, not an empty dict.
  6. Via a real login: the digest shows on the first page load of a session and NOT
     on a second page load in the same session (watermark already advanced).
  7. Nothing here ever calls into app.notify.notify — in-app only, per #235's explicit
     "no push" scope decision.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

USER_ID = 1001
ANIME_A = 9001
ANIME_B = 9002


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


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM sync_log")
        cur.execute("DELETE FROM recommendation_scores")
        cur.execute("DELETE FROM notified_episodes")
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM users")


@pytest.fixture()
def app_module(pg_conn, monkeypatch):
    """Import app.main lazily (after DATABASE_URL/SESSION_SECRET_KEY are set), same
    pattern as tests/test_streaming_availability_notify.py and tests/test_totp_2fa.py."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    return m


def _insert_user(pg_conn, user_id, digest_last_seen_at=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, digest_last_seen_at) "
            "VALUES (%s, 'local', %s, %s, 'x', %s)",
            (user_id, f"user{user_id}", f"user{user_id}@example.com", digest_last_seen_at),
        )


def _insert_anime(pg_conn, anime_id, title):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (%s, %s) "
            "ON CONFLICT (id) DO UPDATE SET title_romaji = EXCLUDED.title_romaji",
            (anime_id, title),
        )


def _insert_library_entry(pg_conn, user_id, anime_id, status):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, anime_id) DO UPDATE SET status = EXCLUDED.status",
            (user_id, anime_id, status),
        )


def _insert_notified_episode(pg_conn, user_id, anime_id, episode, notified_at):
    """Seeds notified_episodes directly with an explicit notified_at, standing in for
    what the real hourly job (_check_airing_episodes) would have written the moment it
    detected the episode airing — this is the digest's actual data source (see
    _build_whats_new_digest's docstring for why not airing_schedule_cache, which is
    deleted+reinserted hourly and essentially never holds a past-dated row in
    production, unlike this table)."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO notified_episodes (user_id, anime_id, episode, notified_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id, anime_id, episode) DO UPDATE SET notified_at = EXCLUDED.notified_at",
            (user_id, anime_id, episode, notified_at),
        )


def _insert_recommendation(pg_conn, user_id, anime_id, score, first_shown_at, dismissed=False, snoozed_until=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, dismissed, snoozed_until, first_shown_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, anime_id, score, dismissed, snoozed_until, first_shown_at),
        )


def _insert_sync_log(pg_conn, user_id, run_at, status, entries_updated, type_="full_sync"):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sync_log (user_id, run_at, type, status, entries_updated) VALUES (%s, %s, %s, %s, %s)",
            (user_id, run_at, type_, status, entries_updated),
        )


NOW = datetime.now(timezone.utc)
YESTERDAY = NOW - timedelta(days=1)
LAST_WEEK = NOW - timedelta(days=7)


# ── _build_whats_new_digest: pure content ───────────────────────────────────────

def test_nothing_new_returns_none(pg_conn, app_module):
    _insert_user(pg_conn, USER_ID)
    digest = app_module._build_whats_new_digest(USER_ID, LAST_WEEK)
    assert digest is None


def test_new_episode_for_watching_show_included(pg_conn, app_module):
    _insert_user(pg_conn, USER_ID)
    _insert_anime(pg_conn, ANIME_A, "Frieren")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "WATCHING")
    _insert_notified_episode(pg_conn, USER_ID, ANIME_A, 5, YESTERDAY)

    digest = app_module._build_whats_new_digest(USER_ID, LAST_WEEK)

    assert digest is not None
    assert len(digest["episodes"]) == 1
    ep = digest["episodes"][0]
    assert ep["title"] == "Frieren"
    assert ep["episodes_aired"] == 1
    assert ep["latest_episode"] == 5


def test_new_episode_for_planning_show_included(pg_conn, app_module):
    # PLANNING is in scope too, matching _check_airing_episodes' own status filter
    # (#256/#257's Upcoming view already treats PLANNING episodes as relevant, not
    # just WATCHING).
    _insert_user(pg_conn, USER_ID)
    _insert_anime(pg_conn, ANIME_A, "Frieren")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "PLANNING")
    _insert_notified_episode(pg_conn, USER_ID, ANIME_A, 1, YESTERDAY)

    digest = app_module._build_whats_new_digest(USER_ID, LAST_WEEK)

    assert digest is not None
    assert len(digest["episodes"]) == 1
    assert digest["episodes"][0]["title"] == "Frieren"


def test_episode_before_watermark_excluded(pg_conn, app_module):
    _insert_user(pg_conn, USER_ID)
    _insert_anime(pg_conn, ANIME_A, "Frieren")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "WATCHING")
    _insert_notified_episode(pg_conn, USER_ID, ANIME_A, 5, LAST_WEEK - timedelta(days=1))  # before the watermark

    digest = app_module._build_whats_new_digest(USER_ID, LAST_WEEK)
    assert digest is None


def test_episode_for_completed_show_excluded(pg_conn, app_module):
    _insert_user(pg_conn, USER_ID)
    _insert_anime(pg_conn, ANIME_A, "Frieren")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "COMPLETED")
    _insert_notified_episode(pg_conn, USER_ID, ANIME_A, 5, YESTERDAY)

    digest = app_module._build_whats_new_digest(USER_ID, LAST_WEEK)
    assert digest is None


def test_multiple_new_episodes_same_show_aggregated(pg_conn, app_module):
    _insert_user(pg_conn, USER_ID)
    _insert_anime(pg_conn, ANIME_A, "Frieren")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "WATCHING")
    _insert_notified_episode(pg_conn, USER_ID, ANIME_A, 5, LAST_WEEK + timedelta(days=1))
    _insert_notified_episode(pg_conn, USER_ID, ANIME_A, 6, YESTERDAY)

    digest = app_module._build_whats_new_digest(USER_ID, LAST_WEEK)

    assert len(digest["episodes"]) == 1
    assert digest["episodes"][0]["episodes_aired"] == 2
    assert digest["episodes"][0]["latest_episode"] == 6


def test_new_recommendations_counted_and_top_titles_surfaced(pg_conn, app_module):
    _insert_user(pg_conn, USER_ID)
    _insert_anime(pg_conn, ANIME_A, "Frieren")
    _insert_anime(pg_conn, ANIME_B, "Bocchi the Rock")
    _insert_recommendation(pg_conn, USER_ID, ANIME_A, 90.0, YESTERDAY)
    _insert_recommendation(pg_conn, USER_ID, ANIME_B, 80.0, YESTERDAY)

    digest = app_module._build_whats_new_digest(USER_ID, LAST_WEEK)

    assert digest["recommendations_count"] == 2
    assert digest["recommendation_titles"] == ["Frieren", "Bocchi the Rock"]


def test_dismissed_and_snoozed_recommendations_excluded(pg_conn, app_module):
    _insert_user(pg_conn, USER_ID)
    _insert_anime(pg_conn, ANIME_A, "Frieren")
    _insert_anime(pg_conn, ANIME_B, "Bocchi the Rock")
    _insert_recommendation(pg_conn, USER_ID, ANIME_A, 90.0, YESTERDAY, dismissed=True)
    _insert_recommendation(pg_conn, USER_ID, ANIME_B, 80.0, YESTERDAY, snoozed_until=NOW + timedelta(days=30))

    digest = app_module._build_whats_new_digest(USER_ID, LAST_WEEK)
    assert digest is None


def test_recommendation_before_watermark_excluded(pg_conn, app_module):
    _insert_user(pg_conn, USER_ID)
    _insert_anime(pg_conn, ANIME_A, "Frieren")
    _insert_recommendation(pg_conn, USER_ID, ANIME_A, 90.0, LAST_WEEK - timedelta(days=1))

    digest = app_module._build_whats_new_digest(USER_ID, LAST_WEEK)
    assert digest is None


def test_sync_activity_summarized(pg_conn, app_module):
    _insert_user(pg_conn, USER_ID)
    _insert_sync_log(pg_conn, USER_ID, YESTERDAY, "ok", 12)
    _insert_sync_log(pg_conn, USER_ID, YESTERDAY, "ok", 3)

    digest = app_module._build_whats_new_digest(USER_ID, LAST_WEEK)

    assert digest["sync_runs"] == 2
    assert digest["sync_entries_updated"] == 15
    assert digest["sync_had_error"] is False


def test_sync_error_flagged(pg_conn, app_module):
    _insert_user(pg_conn, USER_ID)
    _insert_sync_log(pg_conn, USER_ID, YESTERDAY, "error", None)

    digest = app_module._build_whats_new_digest(USER_ID, LAST_WEEK)

    assert digest["sync_runs"] == 1
    assert digest["sync_had_error"] is True


def test_recommender_sync_log_rows_excluded_to_avoid_double_counting(pg_conn, app_module):
    # 'recommender' sync_log rows would double-count content already surfaced via
    # recommendation_scores.first_shown_at above — only full_sync/force_full_resync
    # count toward the sync-activity summary.
    _insert_user(pg_conn, USER_ID)
    _insert_sync_log(pg_conn, USER_ID, YESTERDAY, "ok", 5, type_="recommender")

    digest = app_module._build_whats_new_digest(USER_ID, LAST_WEEK)
    assert digest is None


# ── _whats_new_for_request: session-gated wiring + bootstrap ───────────────────

def test_bootstrap_new_user_gets_no_digest_but_records_watermark(pg_conn, app_module):
    _insert_user(pg_conn, USER_ID, digest_last_seen_at=None)
    _insert_anime(pg_conn, ANIME_A, "Frieren")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "WATCHING")
    _insert_notified_episode(pg_conn, USER_ID, ANIME_A, 5, YESTERDAY)

    class FakeSession(dict):
        pass

    class FakeRequest:
        session = FakeSession()

    digest = app_module._whats_new_for_request(FakeRequest(), {"id": USER_ID}, None)

    assert digest is None
    row = pg_conn.cursor()
    row.execute("SELECT digest_last_seen_at FROM users WHERE id = %s", (USER_ID,))
    (watermark,) = row.fetchone()
    assert watermark is not None


def test_impersonation_skips_digest_entirely(pg_conn, app_module):
    _insert_user(pg_conn, USER_ID, digest_last_seen_at=LAST_WEEK)
    _insert_anime(pg_conn, ANIME_A, "Frieren")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "WATCHING")
    _insert_notified_episode(pg_conn, USER_ID, ANIME_A, 5, YESTERDAY)

    class FakeSession(dict):
        pass

    class FakeRequest:
        session = FakeSession()

    digest = app_module._whats_new_for_request(
        FakeRequest(), {"id": USER_ID}, {"admin_id": 2, "admin_email": "a@example.com"}
    )

    assert digest is None
    row = pg_conn.cursor()
    row.execute("SELECT digest_last_seen_at FROM users WHERE id = %s", (USER_ID,))
    (watermark,) = row.fetchone()
    # untouched — impersonation must never advance the real owner's watermark
    assert watermark == LAST_WEEK


# ── End-to-end via a real login + TestClient ────────────────────────────────────

@pytest.fixture()
def client(app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


def test_digest_shows_once_per_session_via_real_login(pg_conn, app_module, client):
    password = "correct horse battery staple"
    pw_hash = app_module.bcrypt.hashpw(password.encode("utf-8"), app_module.bcrypt.gensalt()).decode("utf-8")
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, digest_last_seen_at) "
            "VALUES (%s, 'local', %s, %s, %s, %s)",
            (USER_ID, f"user{USER_ID}@example.com", f"user{USER_ID}@example.com", pw_hash, LAST_WEEK),
        )
    _insert_anime(pg_conn, ANIME_A, "Frieren")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "WATCHING")
    _insert_notified_episode(pg_conn, USER_ID, ANIME_A, 5, YESTERDAY)

    resp = client.post(
        "/auth/login",
        data={"email": f"user{USER_ID}@example.com", "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 307)

    first_page = client.get("/")
    assert first_page.status_code == 200
    assert "Frieren" in first_page.text
    assert "whats-new-banner" in first_page.text

    second_page = client.get("/")
    assert second_page.status_code == 200
    assert "whats-new-banner" not in second_page.text


def test_digest_never_calls_the_push_notify_dispatcher(pg_conn, app_module, client, monkeypatch):
    # Issue #235's explicit "no push" scope decision — nothing in the digest path
    # may call app.notify.notify.
    called = []
    monkeypatch.setattr(app_module, "notify", lambda *a, **k: called.append((a, k)))

    password = "correct horse battery staple"
    pw_hash = app_module.bcrypt.hashpw(password.encode("utf-8"), app_module.bcrypt.gensalt()).decode("utf-8")
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, digest_last_seen_at) "
            "VALUES (%s, 'local', %s, %s, %s, %s)",
            (USER_ID, f"user{USER_ID}@example.com", f"user{USER_ID}@example.com", pw_hash, LAST_WEEK),
        )
    _insert_anime(pg_conn, ANIME_A, "Frieren")
    _insert_library_entry(pg_conn, USER_ID, ANIME_A, "WATCHING")
    _insert_notified_episode(pg_conn, USER_ID, ANIME_A, 5, YESTERDAY)

    client.post(
        "/auth/login",
        data={"email": f"user{USER_ID}@example.com", "password": password},
        follow_redirects=False,
    )
    client.get("/")

    assert called == []
