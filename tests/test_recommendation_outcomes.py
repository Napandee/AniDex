"""
Coverage for issue #185 — recommend->outcome hit-rate + dismiss-reason distribution.

The recommender has never had a feedback loop: `recommendation_scores.dismissed`/
`dismiss_reason` was captured but never aggregated, and there was no positive signal
linking "we recommended X" to "the user later added/rated X". This suite covers
app.main's `_compute_recommendation_outcomes()` (the new hit-rate + dismiss-reason
query) and `recommendation_scores.first_shown_at` (migration 017 — the anchor the hit
window is measured from, since `computed_at` gets bumped to now() on every recommender
rerun and would otherwise make a long-lived recommendation look "just recommended").

Verified against a real Postgres, same pattern as tests/test_recommendation_snooze.py
and tests/test_seasonal_recommendations.py: this exercises the actual SQL against the
real schema.sql shape, not a hand-duplicated subset that could drift. Needs a reachable
Postgres via DATABASE_URL — skipped entirely if one isn't available.

Covers the acceptance-criteria scenarios from issue #185:
  1. A recommendation followed by the user adding/rating that anime within the hit
     window counts as a hit.
  2. One that doesn't (no library entry, or outside the window, or DROPPED) doesn't.
  3. Dismissed recommendations are never miscounted as hits, even if the anime was
     later added anyway.
  4. The dismiss-reason distribution aggregates correctly across multiple reasons.
  5. first_shown_at survives a recommender rebuild (score_and_store), same
     preservation pattern as `dismissed`/`snoozed_until`.
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
OTHER_USER_ID = 2
ANIME_A = 601
ANIME_B = 602
ANIME_C = 603


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
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
            "VALUES (%s, 'local', 'test', 'test@example.com')",
            (USER_ID,),
        )
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
            "VALUES (%s, 'local', 'test2', 'test2@example.com')",
            (OTHER_USER_ID,),
        )
        for aid in (ANIME_A, ANIME_B, ANIME_C):
            cur.execute(
                "INSERT INTO anime (id, title_romaji) VALUES (%s, %s)",
                (aid, f"Test Anime {aid}"),
            )
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM recommendation_scores")
        cur.execute("DELETE FROM library_entries")


@pytest.fixture()
def compute_outcomes(pg_conn, monkeypatch):
    """Import app.main lazily (after DATABASE_URL/SESSION_SECRET_KEY are set) and
    return its _compute_recommendation_outcomes function."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    return m._compute_recommendation_outcomes


@pytest.fixture()
def score_and_store(pg_conn):
    import run_recommender as rr

    return rr.score_and_store


def _insert_recommendation(pg_conn, anime_id, **overrides):
    row = {
        "score": 80.0,
        "dismissed": False,
        "dismiss_reason": None,
        "first_shown_at_sql": "now()",
    }
    row.update(overrides)
    with pg_conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO recommendation_scores
                (user_id, anime_id, score, dismissed, dismiss_reason, first_shown_at)
            VALUES (%s, %s, %s, %s, %s, {row['first_shown_at_sql']})
            """,
            (USER_ID, anime_id, row["score"], row["dismissed"], row["dismiss_reason"]),
        )


def _insert_library_entry(pg_conn, anime_id, status, **overrides):
    row = {
        "score": None,
        "synced_at_sql": "now()",
        "anilist_updated_at_sql": "NULL",
        "user_id": USER_ID,
    }
    row.update(overrides)
    with pg_conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO library_entries (user_id, anime_id, status, score, synced_at, anilist_updated_at)
            VALUES (%s, %s, %s, %s, {row['synced_at_sql']}, {row['anilist_updated_at_sql']})
            """,
            (row["user_id"], anime_id, status, row["score"]),
        )


# ── No data yet ───────────────────────────────────────────────────────────────

def test_returns_none_when_no_recommendations_ever(compute_outcomes):
    assert compute_outcomes(USER_ID) is None


# ── Hit detection ────────────────────────────────────────────────────────────

def test_hit_when_added_within_window(pg_conn, compute_outcomes):
    _insert_recommendation(pg_conn, ANIME_A)
    _insert_library_entry(pg_conn, ANIME_A, "WATCHING")

    result = compute_outcomes(USER_ID)
    assert result["eligible"] == 1
    assert result["hits"] == 1
    assert result["hit_rate"] == 100


def test_hit_counts_planning_and_completed_statuses(pg_conn, compute_outcomes):
    _insert_recommendation(pg_conn, ANIME_A)
    _insert_recommendation(pg_conn, ANIME_B)
    _insert_library_entry(pg_conn, ANIME_A, "PLANNING")
    _insert_library_entry(pg_conn, ANIME_B, "COMPLETED")

    result = compute_outcomes(USER_ID)
    assert result["eligible"] == 2
    assert result["hits"] == 2


def test_not_a_hit_with_no_library_entry(pg_conn, compute_outcomes):
    _insert_recommendation(pg_conn, ANIME_A)  # never added

    result = compute_outcomes(USER_ID)
    assert result["eligible"] == 1
    assert result["hits"] == 0
    assert result["hit_rate"] == 0


def test_not_a_hit_outside_the_window(pg_conn, compute_outcomes):
    # Recommended 45 days ago, only added just now — 45 days after being first
    # recommended, well past HIT_WINDOW_DAYS (30), so it must not count as a hit.
    _insert_recommendation(pg_conn, ANIME_A, first_shown_at_sql="now() - interval '45 days'")
    _insert_library_entry(pg_conn, ANIME_A, "WATCHING", synced_at_sql="now()")

    result = compute_outcomes(USER_ID)
    assert result["eligible"] == 1
    assert result["hits"] == 0


def test_not_a_hit_when_dropped(pg_conn, compute_outcomes):
    # Added, but DROPPED — issue #185 scope explicitly names WATCHING/PLANNING/
    # COMPLETED as the hit statuses, not DROPPED.
    _insert_recommendation(pg_conn, ANIME_A)
    _insert_library_entry(pg_conn, ANIME_A, "DROPPED")

    result = compute_outcomes(USER_ID)
    assert result["hits"] == 0


def test_hit_uses_anilist_updated_at_when_present(pg_conn, compute_outcomes):
    # anilist_updated_at (AniList's own last-modified timestamp) takes priority
    # over synced_at when both are present — even if synced_at alone would fall
    # outside the window, a recent anilist_updated_at should still count.
    _insert_recommendation(pg_conn, ANIME_A, first_shown_at_sql="now() - interval '5 days'")
    _insert_library_entry(
        pg_conn, ANIME_A, "WATCHING",
        synced_at_sql="now() - interval '100 days'",
        anilist_updated_at_sql="now() - interval '4 days'",
    )

    result = compute_outcomes(USER_ID)
    assert result["hits"] == 1


def test_hits_rated_highly_flag(pg_conn, compute_outcomes):
    _insert_recommendation(pg_conn, ANIME_A)
    _insert_recommendation(pg_conn, ANIME_B)
    _insert_library_entry(pg_conn, ANIME_A, "COMPLETED", score=4.5)
    _insert_library_entry(pg_conn, ANIME_B, "COMPLETED", score=2.0)

    result = compute_outcomes(USER_ID)
    assert result["hits"] == 2
    assert result["hits_rated_highly"] == 1


# ── Dismissed recommendations must never be miscounted as hits ─────────────────

def test_dismissed_recommendation_excluded_from_hit_count_even_if_later_added(pg_conn, compute_outcomes):
    _insert_recommendation(pg_conn, ANIME_A, dismissed=True, dismiss_reason="not_interested")
    # User dismissed it, but later added it anyway — this must not count as a hit,
    # and must not appear in the eligible denominator either.
    _insert_library_entry(pg_conn, ANIME_A, "WATCHING")

    result = compute_outcomes(USER_ID)
    assert result["eligible"] == 0
    assert result["hits"] == 0
    assert result["hit_rate"] is None
    assert result["dismissed_total"] == 1


def test_mixed_dismissed_and_eligible_recommendations(pg_conn, compute_outcomes):
    _insert_recommendation(pg_conn, ANIME_A)  # eligible, hit
    _insert_library_entry(pg_conn, ANIME_A, "WATCHING")
    _insert_recommendation(pg_conn, ANIME_B, dismissed=True, dismiss_reason="wrong_genre")  # excluded
    _insert_recommendation(pg_conn, ANIME_C)  # eligible, not a hit

    result = compute_outcomes(USER_ID)
    assert result["eligible"] == 2
    assert result["hits"] == 1
    assert result["hit_rate"] == 50
    assert result["dismissed_total"] == 1


# ── Dismiss-reason distribution ─────────────────────────────────────────────────

def test_dismiss_reason_distribution_aggregates_across_reasons(pg_conn, compute_outcomes):
    reasons = [
        "not_interested", "not_interested", "not_interested",
        "already_watched", "already_watched",
        "wrong_genre",
        "too_long",
    ]
    with pg_conn.cursor() as cur:
        for i, reason in enumerate(reasons):
            aid = 700 + i
            cur.execute("INSERT INTO anime (id, title_romaji) VALUES (%s, %s)", (aid, f"Dismiss test {aid}"))
            cur.execute(
                "INSERT INTO recommendation_scores (user_id, anime_id, score, dismissed, dismiss_reason) "
                "VALUES (%s, %s, 50.0, true, %s)",
                (USER_ID, aid, reason),
            )

    result = compute_outcomes(USER_ID)
    by_reason = {r["reason"]: r["count"] for r in result["dismiss_reasons"]}
    assert by_reason == {
        "not_interested": 3,
        "already_watched": 2,
        "wrong_genre": 1,
        "too_long": 1,
    }
    assert result["dismissed_total"] == 7


def test_dismiss_reason_no_reason_bucket_for_null_and_blank(pg_conn, compute_outcomes):
    _insert_recommendation(pg_conn, ANIME_A, dismissed=True, dismiss_reason=None)
    _insert_recommendation(pg_conn, ANIME_B, dismissed=True, dismiss_reason="")

    result = compute_outcomes(USER_ID)
    by_reason = {r["reason"]: r["count"] for r in result["dismiss_reasons"]}
    assert by_reason == {"no_reason": 2}


def test_outcomes_scoped_to_the_requesting_user_only(pg_conn, compute_outcomes):
    _insert_recommendation(pg_conn, ANIME_A)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, dismissed) "
            "VALUES (%s, %s, 90.0, true)",
            (OTHER_USER_ID, ANIME_B),
        )

    result = compute_outcomes(USER_ID)
    assert result["eligible"] == 1
    assert result["dismissed_total"] == 0  # the other user's dismissed row must not leak in


# ── first_shown_at preservation across recommender reruns ──────────────────────

def test_recommender_rebuild_preserves_first_shown_at(pg_conn, score_and_store):
    """A recommender rebuild (score_and_store) must not reset first_shown_at —
    same preservation guarantee as dismissed/snoozed_until, extended by #185 since
    the hit-rate window is measured from it. If this regresses, a candidate that
    keeps getting rescored every week would look 'just recommended' forever and the
    hit-rate metric would be meaningless."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, dismissed, first_shown_at) "
            "VALUES (%s, %s, 50.0, false, now() - interval '20 days')",
            (USER_ID, ANIME_A),
        )
        cur.execute("SELECT first_shown_at FROM recommendation_scores WHERE anime_id = %s", (ANIME_A,))
        original_first_shown_at = cur.fetchone()[0]

    import run_recommender as rr

    old_user_id = rr.USER_ID
    rr.USER_ID = USER_ID
    try:
        n = score_and_store(pg_conn, {ANIME_A}, {"genres": {}, "tags": {}, "studios": {}})
    finally:
        rr.USER_ID = old_user_id

    assert n == 1
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT score, computed_at, first_shown_at FROM recommendation_scores WHERE anime_id = %s",
            (ANIME_A,),
        )
        new_score, new_computed_at, new_first_shown_at = cur.fetchone()

    # score/computed_at got rewritten by the rebuild (proves the rebuild actually ran)...
    assert new_score is not None
    # ...but first_shown_at itself is untouched, not reset to the rebuild's now().
    assert new_first_shown_at == original_first_shown_at
    assert new_computed_at != original_first_shown_at


def test_fresh_insert_sets_first_shown_at(pg_conn, score_and_store):
    import run_recommender as rr

    old_user_id = rr.USER_ID
    rr.USER_ID = USER_ID
    try:
        n = score_and_store(pg_conn, {ANIME_A}, {"genres": {}, "tags": {}, "studios": {}})
    finally:
        rr.USER_ID = old_user_id

    assert n == 1
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT first_shown_at IS NOT NULL FROM recommendation_scores WHERE anime_id = %s",
            (ANIME_A,),
        )
        assert cur.fetchone()[0] is True
