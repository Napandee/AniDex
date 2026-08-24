"""
Coverage for `_data_quality_signals()` (issue #202) — the read-only admin Data
Quality tab that aggregates existing sync/data-integrity signals: last successful
sync per provider, recent sync failure rate/history, orphaned personal_notes rows,
stale (already-in-library) recommendation_scores rows, and drift candidates.

Verified against a real Postgres, same pattern as tests/test_admin_instance_health.py
and tests/test_streaming_coverage.py: this exercises the actual query logic in
app.main against the real schema.sql shape, not a hand-duplicated subset of it that
could drift.

Needs a reachable Postgres via DATABASE_URL (the same throwaway-Postgres pattern
.github/workflows/pr-validate.yml provisions) — skipped entirely if one isn't
available, so `pytest tests/` still collects and passes on a machine with no
Postgres running.

One scenario per acceptance-criteria signal from #202:
  1. A known last-successful full_sync run (with a `steps` breakdown) surfaces as
     that user/provider's last_ok_at, and a more recent *failed* attempt for the
     same provider is reported separately as the latest attempt without clobbering
     the last-successful timestamp.
  2. A known mix of ok/error full_sync runs in the lookback window aggregates into
     the correct total/failed/failure_rate, with the failed runs listed for
     drill-down.
  3. A personal_notes row with no matching library_entries row is detected as
     orphaned; a personal_notes row that DOES have a matching library_entries row
     is not.
  4. A recommendation_scores row whose anime_id now has a matching library_entries
     row is detected as a stale recommendation; a dismissed one is excluded; an
     ordinary (still-unwatched) recommendation candidate is not flagged.
  5. A library_entries row stuck (anilist_updated_at older than the threshold) in
     an actively-progressing status is detected as a drift candidate; a similarly
     stale COMPLETED row is not (not an actively-progressing status); a WATCHING
     row updated within the threshold is not.
  6. (Issue #337) Passing user_id restricts every section above to that one
     user's own rows, with no cross-user leakage in either direction, and the
     unscoped (user_id=None) admin path is unaffected.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

USER_A = 1
USER_B = 2


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
    yield conn
    conn.close()


@pytest.fixture()
def data_quality(pg_conn, monkeypatch):
    """Import app.main lazily (after DATABASE_URL/SESSION_SECRET_KEY are set by
    conftest.py) and return its _data_quality_signals function, with the two
    users this suite seeds against already inserted."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
            "VALUES (%s, 'local', 'a@example.com', 'a@example.com') "
            "ON CONFLICT (id) DO NOTHING",
            (USER_A,),
        )
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
            "VALUES (%s, 'local', 'b@example.com', 'b@example.com') "
            "ON CONFLICT (id) DO NOTHING",
            (USER_B,),
        )

    return m._data_quality_signals


def _insert_anime(pg_conn, anime_id: int, title: str = "Test Anime"):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (anime_id, title),
        )


def _insert_library_entry(
    pg_conn, user_id: int, anime_id: int, status: str, anilist_updated_at
):
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO library_entries (user_id, anime_id, status, anilist_updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, anime_id) DO UPDATE SET
                status = EXCLUDED.status, anilist_updated_at = EXCLUDED.anilist_updated_at
            """,
            (user_id, anime_id, status, anilist_updated_at),
        )


def _insert_sync_log(
    pg_conn, user_id: int, run_at, status: str, sync_type: str = "full_sync", steps=None, error_msg=None
):
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_log (user_id, run_at, type, status, error_msg, steps)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (user_id, run_at, sync_type, status, error_msg, psycopg2.extras.Json(steps) if steps is not None else None),
        )


NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 1. Last successful sync per provider
# ---------------------------------------------------------------------------

def test_last_successful_sync_surfaces_per_provider(pg_conn, data_quality):
    ok_at = NOW - timedelta(days=2)
    failed_at = NOW - timedelta(hours=1)

    _insert_sync_log(
        pg_conn, USER_A, ok_at, "ok",
        steps=[
            {"service": "crunchyroll", "status": "ok"},
            {"service": "netflix", "status": "skipped"},
            {"service": "anilist_postgres", "status": "ok"},
        ],
    )
    # A more recent run where the crunchyroll step itself failed — the last
    # *successful* crunchyroll sync should still report ok_at, not this run.
    _insert_sync_log(
        pg_conn, USER_A, failed_at, "partial",
        steps=[
            {"service": "crunchyroll", "status": "error"},
            {"service": "netflix", "status": "skipped"},
            {"service": "anilist_postgres", "status": "ok"},
        ],
    )

    result = data_quality()
    cr = result["last_sync_by_provider"][USER_A]["crunchyroll"]
    assert cr["last_ok_at"] == ok_at
    assert cr["last_attempt_at"] == failed_at
    assert cr["last_attempt_status"] == "error"

    # netflix never succeeded (only ever 'skipped' — not configured for this user)
    netflix = result["last_sync_by_provider"][USER_A]["netflix"]
    assert "last_ok_at" not in netflix

    # A user with no sync_log rows at all has no entry.
    assert USER_B not in result["last_sync_by_provider"]


# ---------------------------------------------------------------------------
# 2. Recent sync failure rate/history
# ---------------------------------------------------------------------------

def test_failure_history_aggregates_correctly(pg_conn, data_quality):
    base = NOW - timedelta(days=1)
    _insert_sync_log(pg_conn, USER_B, base, "ok")
    _insert_sync_log(pg_conn, USER_B, base + timedelta(hours=1), "error", error_msg="boom 1")
    _insert_sync_log(pg_conn, USER_B, base + timedelta(hours=2), "error", error_msg="boom 2")
    _insert_sync_log(pg_conn, USER_B, base + timedelta(hours=3), "ok")
    # Outside the lookback window — must not be counted.
    _insert_sync_log(pg_conn, USER_B, NOW - timedelta(days=90), "error", error_msg="ancient")
    # A recommender run must not be counted (only full_sync/force_full_resync).
    _insert_sync_log(pg_conn, USER_B, base, "error", sync_type="recommender", error_msg="not a sync")

    result = data_quality()
    agg = result["failure_history"][USER_B]
    assert agg["total"] == 4
    assert agg["failed"] == 2
    assert agg["failure_rate"] == pytest.approx(0.5)
    assert {f["error_msg"] for f in agg["failures"]} == {"boom 1", "boom 2"}


# ---------------------------------------------------------------------------
# 3. Orphaned personal_notes
# ---------------------------------------------------------------------------

def test_orphaned_personal_notes_detected(pg_conn, data_quality):
    _insert_anime(pg_conn, 100, "Orphan Note Anime")
    _insert_anime(pg_conn, 101, "Attached Note Anime")
    _insert_library_entry(pg_conn, USER_A, 101, "WATCHING", NOW)

    with pg_conn.cursor() as cur:
        # Orphaned: no library_entries row for (USER_A, 100).
        cur.execute(
            "INSERT INTO personal_notes (user_id, anime_id, notes) VALUES (%s, %s, %s)",
            (USER_A, 100, "orphaned note"),
        )
        # Not orphaned: library_entries row exists for (USER_A, 101).
        cur.execute(
            "INSERT INTO personal_notes (user_id, anime_id, notes) VALUES (%s, %s, %s)",
            (USER_A, 101, "attached note"),
        )

    result = data_quality()
    orphaned_ids = {(r["user_id"], r["anime_id"]) for r in result["orphaned_personal_notes"]}
    assert (USER_A, 100) in orphaned_ids
    assert (USER_A, 101) not in orphaned_ids


# ---------------------------------------------------------------------------
# 4. Stale recommendation_scores
# ---------------------------------------------------------------------------

def test_stale_recommendations_detected(pg_conn, data_quality):
    _insert_anime(pg_conn, 200, "Now In Library")
    _insert_anime(pg_conn, 201, "Still Unwatched")
    _insert_anime(pg_conn, 202, "Now In Library But Dismissed")

    # 200: candidate later added to the library -> stale.
    _insert_library_entry(pg_conn, USER_A, 200, "WATCHING", NOW)
    # 202: candidate later added to the library, but the recommendation was
    # dismissed -> should not be surfaced as stale (already handled by the user).
    _insert_library_entry(pg_conn, USER_A, 202, "PLANNING", NOW)

    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, dismissed) "
            "VALUES (%s, %s, %s, false)",
            (USER_A, 200, 42.0),
        )
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, dismissed) "
            "VALUES (%s, %s, %s, false)",
            (USER_A, 201, 10.0),
        )
        cur.execute(
            "INSERT INTO recommendation_scores (user_id, anime_id, score, dismissed) "
            "VALUES (%s, %s, %s, true)",
            (USER_A, 202, 5.0),
        )

    result = data_quality()
    stale_ids = {(r["user_id"], r["anime_id"]) for r in result["stale_recommendations"]}
    assert (USER_A, 200) in stale_ids
    assert (USER_A, 201) not in stale_ids  # still a genuinely unwatched candidate
    assert (USER_A, 202) not in stale_ids  # dismissed, not surfaced


# ---------------------------------------------------------------------------
# 5. Drift candidates
# ---------------------------------------------------------------------------

def test_drift_candidates_detected(pg_conn, data_quality):
    _insert_anime(pg_conn, 300, "Drifted Watching")
    _insert_anime(pg_conn, 301, "Fresh Watching")
    _insert_anime(pg_conn, 302, "Old Completed")

    stale_ts = NOW - timedelta(days=45)  # older than the 30-day threshold
    fresh_ts = NOW - timedelta(days=2)

    # Drifted: WATCHING, stale anilist_updated_at -> flagged.
    _insert_library_entry(pg_conn, USER_B, 300, "WATCHING", stale_ts)
    # Not drifted: WATCHING, but recently updated.
    _insert_library_entry(pg_conn, USER_B, 301, "WATCHING", fresh_ts)
    # Not drifted: COMPLETED shows aren't expected to keep updating regardless
    # of sync health, even with a very old timestamp.
    _insert_library_entry(pg_conn, USER_B, 302, "COMPLETED", stale_ts)

    result = data_quality()
    drifted_ids = {(r["user_id"], r["anime_id"]) for r in result["drift_candidates"]}
    assert (USER_B, 300) in drifted_ids
    assert (USER_B, 301) not in drifted_ids
    assert (USER_B, 302) not in drifted_ids
    assert result["drift_threshold_days"] == 30


# ---------------------------------------------------------------------------
# 6. user_id scoping (issue #337) — the personal Settings "library health" card
# reuses this exact function with user_id set, rather than duplicating any of
# the queries above. Runs last in this module-scoped-pg_conn file so it can
# lean on every prior test's already-seeded rows: USER_A owns an orphaned note
# (anime 100) and a stale recommendation (anime 200); USER_B owns a drift
# candidate (anime 300) and several sync_log rows. This asserts the *same*
# real data comes back correctly restricted per-user, not synthetic rows.
# ---------------------------------------------------------------------------

def test_user_id_scoping_restricts_every_section_to_one_user(pg_conn, data_quality):
    result_a = data_quality(user_id=USER_A)

    # USER_A's own orphaned note/stale recommendation are still visible...
    assert (USER_A, 100) in {(r["user_id"], r["anime_id"]) for r in result_a["orphaned_personal_notes"]}
    assert (USER_A, 200) in {(r["user_id"], r["anime_id"]) for r in result_a["stale_recommendations"]}
    # ...but nothing belonging to USER_B leaks into a USER_A-scoped call, for
    # any section, even the ones USER_A has zero rows in (drift_candidates).
    for section in ("orphaned_personal_notes", "stale_recommendations", "drift_candidates"):
        assert all(r["user_id"] == USER_A for r in result_a[section]), section
    assert USER_B not in result_a["last_sync_by_provider"]
    assert USER_B not in result_a["failure_history"]

    # And the reverse: a USER_B-scoped call sees USER_B's own drift candidate,
    # but never USER_A's orphaned note or stale recommendation.
    result_b = data_quality(user_id=USER_B)
    assert (USER_B, 300) in {(r["user_id"], r["anime_id"]) for r in result_b["drift_candidates"]}
    for section in ("orphaned_personal_notes", "stale_recommendations", "drift_candidates"):
        assert all(r["user_id"] == USER_B for r in result_b[section]), section
    assert USER_A not in result_b["last_sync_by_provider"]
    assert USER_A not in result_b["failure_history"]

    # The unscoped call (admin path, user_id=None — issue #202's original
    # behavior) is unchanged by #337's addition: still sees both users. Uses
    # failure_history rather than last_sync_by_provider here since only USER_A
    # ever got a sync_log row with a `steps` breakdown (test 1, above) — USER_B's
    # rows (test 2) are plain ok/error runs with no steps, so USER_B correctly
    # never appears in last_sync_by_provider regardless of scoping; that's
    # pre-existing #202 behavior, not something #337 changes.
    result_all = data_quality()
    assert USER_A in result_all["failure_history"]
    assert USER_B in result_all["failure_history"]
