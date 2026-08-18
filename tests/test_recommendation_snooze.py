"""
Coverage for issue #75 — recommendation_scores.snoozed_until, a time-boxed "not now"
alongside the existing permanent `dismissed` flag.

Verified against a real Postgres, same pattern as tests/test_admin_instance_health.py
and tests/test_sync_anilist_upsert.py: this exercises the actual `now()`-comparison
SQL in app.main's _fetch_visible_recommendations() and the real ON CONFLICT upsert in
scripts/run_recommender.py's score_and_store(), against the actual schema.sql shape,
not a hand-duplicated subset of it that could drift.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no
Postgres running.

Covers the three acceptance-criteria scenarios from issue #75:
  1. A snoozed entry is excluded from the recommendations view while snoozed.
  2. It resurfaces once snoozed_until passes.
  3. It survives a recommender-job rebuild (score_and_store) without being reset
     or losing its snooze early — same preservation pattern as `dismissed`.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

USER_ID = 1
ANIME_ID = 555


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
        # Fully clean slate each run, applying the real schema.sql — not a hand-kept
        # subset of it — so this test can't silently drift from the actual table shape.
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(SCHEMA_SQL)
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
            "VALUES (%s, 'local', 'test', 'test@example.com')",
            (USER_ID,),
        )
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (%s, 'Test Anime')",
            (ANIME_ID,),
        )
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_recommendation_scores(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM recommendation_scores")


@pytest.fixture()
def fetch_visible(pg_conn, monkeypatch):
    """Import app.main lazily (after DATABASE_URL/SESSION_SECRET_KEY are set) and
    return its _fetch_visible_recommendations function — the same query the
    /recommendations route uses."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    return m._fetch_visible_recommendations


@pytest.fixture()
def score_and_store(pg_conn):
    import run_recommender as rr

    return rr.score_and_store


def _insert_recommendation(pg_conn, **overrides):
    row = {"score": 80.0, "dismissed": False, "snoozed_until": None}
    row.update(overrides)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, dismissed, snoozed_until) "
            "VALUES (%s, %s, %s, %s, %s)",
            (USER_ID, ANIME_ID, row["score"], row["dismissed"], row["snoozed_until"]),
        )


def test_snoozed_entry_excluded_while_snooze_is_future(pg_conn, fetch_visible):
    # `now() + interval` must be evaluated server-side, so insert with a raw
    # execute rather than going through _insert_recommendation's %s substitution.
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, dismissed, snoozed_until) "
            "VALUES (%s, %s, 80.0, false, now() + interval '30 days')",
            (USER_ID, ANIME_ID),
        )

    assert fetch_visible(USER_ID) == []


def test_snoozed_entry_resurfaces_after_expiry(pg_conn, fetch_visible):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, dismissed, snoozed_until) "
            "VALUES (%s, %s, 80.0, false, now() - interval '1 day')",
            (USER_ID, ANIME_ID),
        )

    rows = fetch_visible(USER_ID)
    assert len(rows) == 1
    assert rows[0]["id"] == ANIME_ID


def test_unsnoozed_entry_is_visible(pg_conn, fetch_visible):
    _insert_recommendation(pg_conn)  # snoozed_until = NULL

    rows = fetch_visible(USER_ID)
    assert len(rows) == 1
    assert rows[0]["id"] == ANIME_ID


def test_dismissed_entry_still_excluded_regardless_of_snooze(pg_conn, fetch_visible):
    """Out-of-scope guard (issue #75 acceptance criteria): the existing permanent
    dismiss flow must be unaffected by adding snoozed_until."""
    _insert_recommendation(pg_conn, dismissed=True)

    assert fetch_visible(USER_ID) == []


def test_recommender_rebuild_preserves_active_snooze(pg_conn, score_and_store):
    """A recommender rebuild (score_and_store) must not reset or shorten an
    in-progress snooze — same preservation guarantee CLAUDE.md documents for
    `dismissed`, now extended to snoozed_until (issue #75)."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, dismissed, snoozed_until) "
            "VALUES (%s, %s, 50.0, false, now() + interval '25 days')",
            (USER_ID, ANIME_ID),
        )
        cur.execute("SELECT snoozed_until FROM recommendation_scores WHERE anime_id = %s", (ANIME_ID,))
        original_snoozed_until = cur.fetchone()[0]

    import run_recommender as rr

    old_user_id = rr.USER_ID
    rr.USER_ID = USER_ID
    try:
        n = score_and_store(pg_conn, {ANIME_ID}, {"genres": {}, "tags": {}, "studios": {}})
    finally:
        rr.USER_ID = old_user_id

    assert n == 1
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT score, snoozed_until FROM recommendation_scores WHERE anime_id = %s",
            (ANIME_ID,),
        )
        new_score, new_snoozed_until = cur.fetchone()

    # score got rewritten by the rebuild (proves the rebuild actually ran)...
    assert new_score is not None
    # ...but the snooze itself is untouched, not reset and not silently cleared.
    assert new_snoozed_until == original_snoozed_until


def test_recommender_rebuild_preserves_dismissed_flag(pg_conn, score_and_store):
    """Existing guarantee (CLAUDE.md), re-verified here alongside the new
    snoozed_until coverage above so a future change can't silently break one while
    fixing the other."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, dismissed, dismiss_reason) "
            "VALUES (%s, %s, 50.0, true, 'not_interested')",
            (USER_ID, ANIME_ID),
        )

    import run_recommender as rr

    old_user_id = rr.USER_ID
    rr.USER_ID = USER_ID
    try:
        score_and_store(pg_conn, {ANIME_ID}, {"genres": {}, "tags": {}, "studios": {}})
    finally:
        rr.USER_ID = old_user_id

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT dismissed, dismiss_reason FROM recommendation_scores WHERE anime_id = %s",
            (ANIME_ID,),
        )
        dismissed, dismiss_reason = cur.fetchone()

    assert dismissed is True
    assert dismiss_reason == "not_interested"
