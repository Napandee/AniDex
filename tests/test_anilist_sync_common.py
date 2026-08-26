import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

import anilist_sync_common as common
from anilist_sync_common import is_plausible_match, seed_search_cache, search_cache_snapshot


# ── Persistent title-search cache seeding (issue #115) ──────────────────────────

def test_seed_search_cache_populates_snapshot(monkeypatch):
    monkeypatch.setattr(common, "_search_cache", {})
    seed_search_cache({"Some Western Show": None, "Attack on Titan": 16498})
    assert search_cache_snapshot() == {"Some Western Show": None, "Attack on Titan": 16498}


def test_seed_search_cache_merges_without_clobbering_existing_entries(monkeypatch):
    monkeypatch.setattr(common, "_search_cache", {"Already Resolved": 123})
    seed_search_cache({"New Title": None})
    assert search_cache_snapshot() == {"Already Resolved": 123, "New Title": None}


def test_search_cache_snapshot_returns_a_copy_not_a_live_reference(monkeypatch):
    monkeypatch.setattr(common, "_search_cache", {"Title": 1})
    snapshot = search_cache_snapshot()
    snapshot["Title"] = 999
    assert common._search_cache["Title"] == 1  # mutating the snapshot didn't affect the real cache


# ── load_user_list_from_db — local-mirror equivalent of fetch_user_list() (issue #99) ──
# Minimal in-memory stand-in for a psycopg2 connection, just for this function's one
# join query.
class _FakeLibraryConn:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        pass  # single fixed query — nothing to branch on

    def fetchall(self):
        return self._rows

    def close(self):
        self.closed = True


def test_load_user_list_from_db_builds_entries_and_title_index(monkeypatch):
    rows = [
        {
            "media_id": 154587, "status": "COMPLETED", "progress": 28, "repeat_count": 0,
            "total_episodes": 28, "format": "TV",
            "title_romaji": "Shingeki no Kyojin", "title_english": "Attack on Titan",
        },
        {
            "media_id": 170068, "status": "CURRENT", "progress": 5, "repeat_count": 0,
            "total_episodes": None, "format": "TV",
            "title_romaji": "Sousou no Frieren", "title_english": None,
        },
    ]
    monkeypatch.setattr(common.psycopg2, "connect", lambda *a, **kw: _FakeLibraryConn(rows))

    entries, title_index = common.load_user_list_from_db()

    assert entries[154587] == {
        "status": "COMPLETED", "progress": 28, "repeat": 0,
        "total_episodes": 28, "format": "TV", "title": "Attack on Titan",
    }
    # No english title — falls back to romaji, matching fetch_user_list()'s own rule.
    assert entries[170068]["title"] == "Sousou no Frieren"
    assert title_index["attack on titan"] == 154587
    assert title_index["shingeki no kyojin"] == 154587
    assert title_index["sousou no frieren"] == 170068


def test_load_user_list_from_db_empty_library(monkeypatch):
    monkeypatch.setattr(common.psycopg2, "connect", lambda *a, **kw: _FakeLibraryConn([]))
    entries, title_index = common.load_user_list_from_db()
    assert entries == {}
    assert title_index == {}


def test_load_user_list_from_db_closes_connection(monkeypatch):
    conn = _FakeLibraryConn([])
    monkeypatch.setattr(common.psycopg2, "connect", lambda *a, **kw: conn)
    common.load_user_list_from_db()
    assert conn.closed is True


def test_movie_watch_against_tv_entry_is_implausible():
    # Live-action Death Note (movie) vs the anime (TV, 37 eps) — the collision
    # case is_plausible_match exists to catch.
    entry = {"format": "TV", "total_episodes": 37}
    assert is_plausible_match(entry, watched_format="MOVIE", watched_episode_count=1) is False


def test_tv_watch_against_tv_entry_is_plausible():
    entry = {"format": "TV", "total_episodes": 24}
    assert is_plausible_match(entry, watched_format="TV", watched_episode_count=5) is True


def test_movie_watch_against_movie_entry_is_plausible():
    entry = {"format": "MOVIE", "total_episodes": 1}
    assert is_plausible_match(entry, watched_format="MOVIE", watched_episode_count=1) is True


def test_episode_count_overrunning_anilist_total_is_implausible():
    entry = {"format": "TV", "total_episodes": 24}
    assert is_plausible_match(entry, watched_format="TV", watched_episode_count=99) is False


def test_missing_format_or_count_info_defaults_to_plausible():
    # Can't rule anything out without data to compare — must not false-positive-reject.
    entry = {"format": None, "total_episodes": None}
    assert is_plausible_match(entry, watched_format=None, watched_episode_count=None) is True


def test_ova_entry_is_not_treated_as_a_movie():
    entry = {"format": "OVA", "total_episodes": 3}
    assert is_plausible_match(entry, watched_format="MOVIE", watched_episode_count=1) is False


# ── enqueue_outbox_update — local-first provider-sync writes (issue #100) ──────
# Minimal in-memory stand-in for a psycopg2 connection/cursor — records every
# executed statement so tests can assert on shape/params without a real DB.
class _FakeOutboxConn:
    def __init__(self):
        self.queries: list[tuple[str, object]] = []
        self.committed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self.queries.append((" ".join(query.split()), params))

    def commit(self):
        self.committed = True


def test_enqueue_outbox_update_requires_at_least_one_field():
    conn = _FakeOutboxConn()
    try:
        common.enqueue_outbox_update(conn, 42, "netflix")
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert conn.queries == []


def test_enqueue_outbox_update_status_only_sets_just_that_column():
    conn = _FakeOutboxConn()
    common.enqueue_outbox_update(conn, 42, "netflix", status="COMPLETED")

    # Issue #252 — the library_entries write is now an upsert (INSERT ... ON
    # CONFLICT DO UPDATE), so it can create a brand-new row too, not just update
    # an existing one. The ON CONFLICT SET clause still only touches the columns
    # actually supplied (matching the old bare-UPDATE behavior for an existing
    # row); the leading VALUES params carry INSERT-branch defaults for whatever
    # wasn't supplied, unused whenever the row already exists.
    update_sql, update_params = conn.queries[0]
    assert "INSERT INTO library_entries" in update_sql
    assert "ON CONFLICT (user_id, anime_id) DO UPDATE SET" in update_sql
    assert "status = %s" in update_sql
    assert "progress = %s" not in update_sql
    assert "repeat_count = %s" not in update_sql
    assert "sync_status = 'pending'" in update_sql
    assert list(update_params) == [common.USER_ID, 42, "COMPLETED", 0, 0, "COMPLETED"]

    insert_sql, insert_params = conn.queries[2]
    assert "INSERT INTO status_sync_outbox" in insert_sql
    assert insert_params == (common.USER_ID, 42, "netflix", "COMPLETED", None, None)


def test_enqueue_outbox_update_progress_and_repeat_without_status():
    conn = _FakeOutboxConn()
    common.enqueue_outbox_update(conn, 42, "crunchyroll", progress=5, repeat=2)

    update_sql, update_params = conn.queries[0]
    assert "progress = %s" in update_sql
    assert "repeat_count = %s" in update_sql
    assert "status = %s" not in update_sql
    # INSERT-branch status defaults to the 'PLANNING' placeholder (never actually
    # persisted here since a row for anime_id 42 already exists in real use) —
    # the ON CONFLICT SET clause omits status entirely, so an existing row's real
    # status is left untouched either way.
    assert list(update_params) == [common.USER_ID, 42, "PLANNING", 5, 2, 5, 2]

    insert_sql, insert_params = conn.queries[2]
    assert insert_params == (common.USER_ID, 42, "crunchyroll", None, 5, 2)


def test_enqueue_outbox_update_creates_new_row_when_none_exists():
    # Issue #252 — the actual new behavior: when no library_entries row exists
    # yet for (user, anime), the INSERT branch fires (no ON CONFLICT), creating
    # one with the caller-supplied status/progress/repeat.
    conn = _FakeOutboxConn()
    common.enqueue_outbox_update(conn, 99, "crunchyroll", status="WATCHING", progress=3)

    insert_sql, insert_params = conn.queries[0]
    assert "INSERT INTO library_entries" in insert_sql
    assert list(insert_params) == [common.USER_ID, 99, "WATCHING", 3, 0, "WATCHING", 3]


def test_enqueue_outbox_update_supersedes_existing_pending_or_failed_row():
    conn = _FakeOutboxConn()
    common.enqueue_outbox_update(conn, 42, "netflix", status="CURRENT")

    delete_sql, delete_params = conn.queries[1]
    assert "DELETE FROM status_sync_outbox" in delete_sql
    assert "state IN" in delete_sql
    assert delete_params == (common.USER_ID, 42)


def test_enqueue_outbox_update_does_not_commit():
    # Runs inside the caller's own transaction (save_nf_state()/save_cr_state()
    # commits both together) — see the function's own docstring for why.
    conn = _FakeOutboxConn()
    common.enqueue_outbox_update(conn, 42, "netflix", status="CURRENT")
    assert conn.committed is False


# ── ensure_anime_stub — issue #252 ──────────────────────────────────────────

def test_ensure_anime_stub_inserts_minimal_row_on_conflict_do_nothing():
    conn = _FakeOutboxConn()
    common.ensure_anime_stub(conn, 20678, "The Testament of Sister New Devil")

    sql, params = conn.queries[0]
    assert "INSERT INTO anime" in sql
    assert "ON CONFLICT (id) DO NOTHING" in sql
    assert params == (20678, "The Testament of Sister New Devil")


# ── resolve_or_create_user_list_entry — issue #252, create-path gate hardened
# by #387 ────────────────────────────────────────────────────────────────────
# The real bug (#252): incremental CR/Netflix sync resolved a title correctly
# (e.g. via the title-search cache) but the anime wasn't on the user's AniList
# list, so it was silently skipped forever. Reproduces issue #252's Context
# section shape: a title resolves to a media_id with no existing
# user_list/library_entries row.
#
# The second bug (#387): the create path used to build its synthetic entry with
# format=None/total_episodes=None regardless of the real candidate, which made
# is_plausible_match()'s checks silently no-op on exactly the highest-risk path.
# resolve_or_create_user_list_entry() now fetches real AniList metadata
# (fetch_anilist_media_metadata()) before deciding to create — every test below
# monkeypatches that function rather than hitting the real AniList API, both for
# test isolation and because a real network dependency in this test would be
# flaky/slow and could fail with no internet in CI.

def _mock_metadata(monkeypatch, result):
    """result is a dict (a "found" real AniList media) or None (fetch failed).
    Also resets the module-level cache so consecutive tests with the same
    media_id don't see a stale cached value from an earlier test."""
    monkeypatch.setattr(common, "_media_metadata_cache", {})
    monkeypatch.setattr(common, "fetch_anilist_media_metadata", lambda media_id: result)


def test_full_pull_unmatched_title_still_skips(monkeypatch):
    # Regression coverage for the deliberately-unchanged case: the initial
    # full-history walk (or a user-triggered Force Full Resync, #20/#21) must
    # NOT auto-create — same behavior as before this issue. full_pull short-
    # circuits before the metadata fetch even happens.
    called = []
    monkeypatch.setattr(common, "fetch_anilist_media_metadata", lambda media_id: called.append(media_id))
    user_list = {}
    conn = _FakeOutboxConn()
    decision = common.resolve_or_create_user_list_entry(
        20678, "The Testament of Sister New Devil", user_list, full_pull=True, conn=conn,
    )
    assert decision == "skip"
    assert user_list == {}
    assert conn.queries == []  # no anime stub written
    assert called == []  # metadata fetch never even attempted


def test_incremental_unmatched_title_creates_synthetic_entry(monkeypatch):
    # The #252 fix: a normal day-to-day incremental sync (full_pull=False) for a
    # title that resolved correctly but isn't tracked yet creates a new entry
    # instead — provided (#387) it also passes real-metadata + title-similarity
    # validation. Real romaji here deliberately matches the searched title
    # closely, so this test isolates the "create when genuinely plausible" path.
    _mock_metadata(monkeypatch, {
        "format": "TV", "episodes": 12,
        "title_romaji": "The Testament of Sister New Devil", "title_english": "",
    })
    user_list = {}
    conn = _FakeOutboxConn()
    decision = common.resolve_or_create_user_list_entry(
        20678, "The Testament of Sister New Devil", user_list, full_pull=False, conn=conn,
    )
    assert decision == "create"
    assert user_list[20678] == {
        "status": None, "progress": 0, "repeat": 0,
        "total_episodes": 12, "format": "TV",
        "title": "The Testament of Sister New Devil",
    }
    # The anime stub is written before the caller's own outbox enqueue, so the
    # library_entries/status_sync_outbox FK constraint on anime_id is satisfied.
    sql, params = conn.queries[0]
    assert "INSERT INTO anime" in sql
    assert params == (20678, "The Testament of Sister New Devil")


def test_incremental_unmatched_title_dry_run_skips_db_write(monkeypatch):
    # DRY_RUN mode (all four provider scripts, #387 Part 2) passes conn=None —
    # no DB write, but the synthetic entry is still added so the rest of
    # DRY_RUN's logging/process() path exercises the same decision a real run
    # would make. The metadata fetch itself is a read — it still happens for
    # real under DRY_RUN (mocked here only for test isolation, same as above).
    _mock_metadata(monkeypatch, {
        "format": "TV", "episodes": 12,
        "title_romaji": "The Testament of Sister New Devil", "title_english": "",
    })
    user_list = {}
    decision = common.resolve_or_create_user_list_entry(
        20678, "The Testament of Sister New Devil", user_list, full_pull=False, conn=None,
    )
    assert decision == "create"
    assert user_list[20678]["status"] is None


def test_already_tracked_title_returns_existing_and_does_not_touch_user_list(monkeypatch):
    # decision == "existing" returns before the metadata fetch — an
    # already-tracked title never needs create-path validation at all.
    called = []
    monkeypatch.setattr(common, "fetch_anilist_media_metadata", lambda media_id: called.append(media_id))
    user_list = {154587: {"status": "CURRENT", "progress": 3, "repeat": 0,
                           "total_episodes": 24, "format": "TV", "title": "Attack on Titan"}}
    conn = _FakeOutboxConn()
    decision = common.resolve_or_create_user_list_entry(
        154587, "Attack on Titan", user_list, full_pull=False, conn=conn,
    )
    assert decision == "existing"
    assert user_list[154587]["status"] == "CURRENT"  # untouched
    assert conn.queries == []
    assert called == []


def test_create_skips_when_metadata_fetch_fails(monkeypatch):
    # A transient AniList API error can't be validated — conservative default is
    # skip, not create (the title is picked up again on the next sync).
    _mock_metadata(monkeypatch, None)
    user_list = {}
    conn = _FakeOutboxConn()
    decision = common.resolve_or_create_user_list_entry(
        99999, "Some Show", user_list, full_pull=False, conn=conn,
    )
    assert decision == "skip"
    assert user_list == {}
    assert conn.queries == []


# ── Real 2026-08-26 incident data (issue #387) — the actual root-cause fix.
# Confirmed live against the real AniList API during that day's investigation;
# hardcoded here as fixtures so the fix is proven against the real failure, not
# just synthetic cases. See TITLE_SIMILARITY_THRESHOLD's own comment in
# anilist_sync_common.py for the full table + why this check has to exist:
# AniList's search always returns its closest guess, never confirms confidence,
# so "found a media_id" alone was never proof it was the RIGHT one.

_INCIDENT_REJECT_CASES = [
    # (watched_title, watched_format, watched_episode_count, real AniList metadata)
    ("Wind River", "MOVIE", 1, {
        "format": "SPECIAL", "episodes": 4,
        "title_romaji": "Otona Joshi no Anime Time", "title_english": "",
    }),
    ("The Guest", "MOVIE", 1, {
        "format": "TV_SHORT", "episodes": 25,
        "title_romaji": "Gregory Horror Show: The Second Guest", "title_english": "",
    }),
    ("The Proposal", "MOVIE", 1, {
        "format": None, "episodes": None,
        "title_romaji": "Ousama no Propose", "title_english": "",
    }),
    ("The Boys", "TV", 8, {
        "format": "TV", "episodes": 40,
        "title_romaji": "Wakakusa Monogatari: Nan to Jo-sensei",
        "title_english": "Little Women II: Jo's Boys",
    }),
]

_INCIDENT_ACCEPT_CASES = [
    ("Beck", "TV", 1, {
        "format": "TV", "episodes": 26,
        "title_romaji": "BECK", "title_english": "Beck: Mongolian Chop Squad",
    }),
    ("Ghost in The Shell: Stand Alone Complex", "TV", 2, {
        "format": "TV", "episodes": 26,
        "title_romaji": "Koukaku Kidoutai: STAND ALONE COMPLEX",
        "title_english": "Ghost in the Shell: Stand Alone Complex",
    }),
]


@pytest.mark.parametrize("watched_title,watched_format,watched_ep,metadata", _INCIDENT_REJECT_CASES)
def test_real_incident_false_positives_are_rejected(monkeypatch, watched_title, watched_format, watched_ep, metadata):
    _mock_metadata(monkeypatch, metadata)
    user_list = {}
    conn = _FakeOutboxConn()
    decision = common.resolve_or_create_user_list_entry(
        12345, watched_title, user_list, full_pull=False, conn=conn,
        watched_format=watched_format, watched_episode_count=watched_ep,
    )
    assert decision == "skip"
    assert user_list == {}
    assert conn.queries == []  # no anime stub written — the ordering fix (#387)


@pytest.mark.parametrize("watched_title,watched_format,watched_ep,metadata", _INCIDENT_ACCEPT_CASES)
def test_real_incident_legitimate_matches_are_accepted(monkeypatch, watched_title, watched_format, watched_ep, metadata):
    _mock_metadata(monkeypatch, metadata)
    user_list = {}
    conn = _FakeOutboxConn()
    decision = common.resolve_or_create_user_list_entry(
        12345, watched_title, user_list, full_pull=False, conn=conn,
        watched_format=watched_format, watched_episode_count=watched_ep,
    )
    assert decision == "create"
    assert user_list[12345]["format"] == metadata["format"]
    assert user_list[12345]["total_episodes"] == metadata["episodes"]


def test_unknown_metadata_with_low_similarity_is_rejected(monkeypatch):
    # "The Proposal" case again, isolated: format/episodes both unknown is
    # itself treated as a signal against creating (a real, already-watched
    # anime almost always has real publisher metadata by now), not neutral.
    _mock_metadata(monkeypatch, {
        "format": None, "episodes": None,
        "title_romaji": "Ousama no Propose", "title_english": "",
    })
    user_list = {}
    decision = common.resolve_or_create_user_list_entry(
        12345, "The Proposal", user_list, full_pull=False, conn=_FakeOutboxConn(),
        watched_format="MOVIE", watched_episode_count=1,
    )
    assert decision == "skip"


def test_unknown_metadata_with_near_exact_title_is_still_accepted(monkeypatch):
    # The stricter UNKNOWN_METADATA_TITLE_SIMILARITY_THRESHOLD is an extra
    # caution, not an unconditional reject — a near-exact title match on an
    # unknown-format/episode candidate should still be trusted.
    _mock_metadata(monkeypatch, {
        "format": None, "episodes": None,
        "title_romaji": "Some Very Specific Show Title", "title_english": "",
    })
    user_list = {}
    decision = common.resolve_or_create_user_list_entry(
        12345, "Some Very Specific Show Title", user_list, full_pull=False, conn=_FakeOutboxConn(),
    )
    assert decision == "create"


def test_existing_path_ignores_title_similarity_entirely(monkeypatch):
    # The title-similarity gate only ever applies to the create decision — an
    # already-tracked entry (the user put it there themselves) is untouched by
    # it, matching is_plausible_match()'s own unchanged behavior for that path.
    called = []
    monkeypatch.setattr(common, "fetch_anilist_media_metadata", lambda media_id: called.append(media_id))
    user_list = {154587: {"status": "CURRENT", "progress": 3, "repeat": 0,
                           "total_episodes": 24, "format": "TV", "title": "Attack on Titan"}}
    decision = common.resolve_or_create_user_list_entry(
        154587, "A Completely Unrelated Search Query", user_list, full_pull=False, conn=_FakeOutboxConn(),
    )
    assert decision == "existing"
    assert called == []  # never even reaches the title-similarity check


# ── _title_similarity / _normalize_title (issue #387) ───────────────────────

def test_title_similarity_exact_match_after_normalization():
    assert common._title_similarity("Beck", ["BECK", None]) == 1.0


def test_title_similarity_ignores_punctuation_and_case():
    assert common._title_similarity(
        "Ghost in The Shell: Stand Alone Complex",
        ["Koukaku Kidoutai: STAND ALONE COMPLEX", "Ghost in the Shell: Stand Alone Complex"],
    ) == 1.0


def test_title_similarity_takes_the_better_of_romaji_or_english():
    # Low similarity to the english alt but exact to romaji — best-of, not
    # first-of or average-of.
    assert common._title_similarity("Beck", ["BECK", "Beck: Mongolian Chop Squad"]) == 1.0


def test_title_similarity_unrelated_titles_score_low():
    assert common._title_similarity("Wind River", ["Otona Joshi no Anime Time", None]) < 0.4


def test_title_similarity_empty_candidates_score_zero():
    assert common._title_similarity("Anything", [None, ""]) == 0.0


def test_title_similarity_empty_watched_title_score_zero():
    assert common._title_similarity("", ["Anything"]) == 0.0


# ── load_walk_complete / set_walk_complete — issue #97/#104, moved to this
# shared module and hardened by #387 (each provider script used to have its
# own copy, and each one's fallback — "if sync-state rows already exist,
# assume the walk completed" — was the actual trigger of the 2026-08-26
# incident). Minimal in-memory stand-in for a psycopg2 connection, same shape
# as the per-script _FakeWalkConn these tests replace.
class _FakeWalkConn:
    def __init__(self, initial_value=None):
        self.value = initial_value  # None = "no row for this key yet"
        self.committed = False
        self._last_select = None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params):
        q = query.strip()
        if q.startswith("SELECT"):
            self._last_select = self.value
        elif q.startswith("INSERT"):
            self.value = params[-1]

    def fetchone(self):
        return {"value": self._last_select} if self._last_select is not None else None

    def commit(self):
        self.committed = True


def test_load_walk_complete_false_when_never_set_even_with_existing_state():
    # Issue #387 — the exact fix: no explicit flag row means "not complete,"
    # full stop, regardless of whether sync-state rows already exist for this
    # provider. This IS the fix for the incident (the old has_existing_state
    # fallback used to return True here).
    assert common.load_walk_complete(_FakeWalkConn(initial_value=None), "primevideo", 1) is False


def test_load_walk_complete_reads_explicit_stored_value():
    assert common.load_walk_complete(_FakeWalkConn(initial_value="true"), "netflix", 1) is True
    assert common.load_walk_complete(_FakeWalkConn(initial_value="false"), "netflix", 1) is False


def test_load_walk_complete_false_when_conn_is_none():
    # DRY_RUN mode — treated as a from-scratch first sync, matching every other
    # DRY_RUN-guarded read.
    assert common.load_walk_complete(None, "plex", 1) is False


def test_set_walk_complete_persists_and_commits():
    conn = _FakeWalkConn()
    common.set_walk_complete(conn, "crunchyroll", 1, True)
    assert conn.value == "true"
    assert conn.committed is True

    common.set_walk_complete(conn, "crunchyroll", 1, False)
    assert conn.value == "false"


def test_set_walk_complete_noop_when_conn_is_none():
    # Must not raise — DRY_RUN passes conn=None through to every state writer.
    common.set_walk_complete(None, "crunchyroll", 1, True)


# ── Season-aware matching (issue #159) ───────────────────────────────────────

def test_season_suffix_candidates_returns_empty_for_season_1_or_less():
    assert common.season_suffix_candidates("Kingdom", 1) == []
    assert common.season_suffix_candidates("Kingdom", 0) == []


def test_season_suffix_candidates_ordinal_and_roman_for_season_2():
    # Concrete #159 test case: Kingdom S2 is "Kingdom 2nd Season" on AniList.
    assert common.season_suffix_candidates("Kingdom", 2) == [
        "Kingdom 2nd Season", "Kingdom Season 2", "Kingdom II",
    ]


def test_season_suffix_candidates_ordinal_for_season_3_and_4():
    assert common.season_suffix_candidates("Overlord", 3) == [
        "Overlord 3rd Season", "Overlord Season 3", "Overlord III",
    ]
    assert common.season_suffix_candidates("Overlord", 4) == [
        "Overlord 4th Season", "Overlord Season 4", "Overlord IV",
    ]


def test_season_suffix_candidates_ordinal_for_teens_uses_th_not_st_nd_rd():
    assert common.season_suffix_candidates("Show", 11)[0] == "Show 11th Season"
    assert common.season_suffix_candidates("Show", 12)[0] == "Show 12th Season"
    assert common.season_suffix_candidates("Show", 13)[0] == "Show 13th Season"


def test_season_suffix_candidates_no_roman_numeral_past_ten():
    candidates = common.season_suffix_candidates("Show", 11)
    assert len(candidates) == 2  # ordinal + "Season N" only, no roman-numeral entry


def test_find_anilist_id_season_aware_matches_index_suffix_before_bare_title(monkeypatch):
    monkeypatch.setattr(common, "_search_cache", {})
    # Both the franchise's bare title (season 1) and the season-2 suffixed title are
    # already indexed — the season-2 CR entry must resolve to the season-2 id, not
    # silently fall through to season 1's, the core #159 bug.
    title_index = {"kingdom": 1, "kingdom 2nd season": 2}
    assert common.find_anilist_id("Kingdom", title_index, season_number=2) == 2


def test_find_anilist_id_season_1_ignores_suffix_heuristic_entirely(monkeypatch):
    monkeypatch.setattr(common, "_search_cache", {})
    title_index = {"kingdom": 1, "kingdom 2nd season": 2}
    assert common.find_anilist_id("Kingdom", title_index, season_number=1) == 1


def test_find_anilist_id_season_aware_searches_suffix_candidates_in_order(monkeypatch):
    monkeypatch.setattr(common, "_search_cache", {})
    title_index = {"kingdom": 1}  # only the bare title is pre-indexed
    searched = []

    def fake_gql(query, variables=None, token=None):
        searched.append(variables["search"])
        if variables["search"] == "Kingdom 2nd Season":
            return {"Media": {"id": 2}}
        raise RuntimeError("not found")

    monkeypatch.setattr(common, "gql", fake_gql)
    assert common.find_anilist_id("Kingdom", title_index, season_number=2) == 2
    assert searched[0] == "Kingdom 2nd Season"  # tried before "Kingdom Season 2"/"Kingdom II"/bare title


def test_find_anilist_id_season_aware_falls_back_to_bare_title_when_no_suffix_matches(monkeypatch):
    monkeypatch.setattr(common, "_search_cache", {})
    title_index = {"kingdom": 1}  # bare title only, no suffix candidate anywhere

    def fake_gql(query, variables=None, token=None):
        raise RuntimeError("no match for any suffix")

    monkeypatch.setattr(common, "gql", fake_gql)
    # Falls all the way back to the bare-title index hit — same ceiling as
    # pre-#159 behavior when the heuristic can't do better; the manual override
    # table exists to cover exactly this case.
    assert common.find_anilist_id("Kingdom", title_index, season_number=2) == 1


def test_find_anilist_id_caches_season_suffix_search_result(monkeypatch):
    monkeypatch.setattr(common, "_search_cache", {})
    title_index = {}
    calls = []

    def fake_gql(query, variables=None, token=None):
        calls.append(variables["search"])
        return {"Media": {"id": 42}}

    monkeypatch.setattr(common, "gql", fake_gql)
    first = common.find_anilist_id("Saga of Tanya the Evil", title_index, season_number=2)
    second = common.find_anilist_id("Saga of Tanya the Evil", title_index, season_number=2)
    assert first == 42
    assert second == 42
    assert calls == ["Saga of Tanya the Evil 2nd Season"]  # only searched once — cached after


# ── enqueue_outbox_update against a real Postgres — status_sync_outbox's source
# CHECK constraint (issue #354/#252 follow-up) ───────────────────────────────
# Every other enqueue_outbox_update() test above uses _FakeOutboxConn, a pure
# in-memory stand-in that never touches a real constraint — which is exactly how
# a real bug shipped and reached production undetected: migrations/010's original
# CHECK (source IN ('ui_bulk_edit', 'crunchyroll', 'netflix', 'prime_video')) used
# the wrong spelling for Prime Video (underscore) and omitted 'plex' entirely,
# while every actual call site (sync_primevideo.py/sync_plex.py) uses the
# no-underscore spelling. The very first live Prime Video sync to create a
# genuinely new AniList entry hit this constraint in production. This section
# exercises the constraint for real, against a real Postgres, for every source
# string an actual call site uses — see migrations/032_fix_outbox_source_check.sql.

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
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(SCHEMA_SQL)
    yield conn
    conn.close()


_next_outbox_user_id = [9000]


def _make_outbox_user(pg_conn):
    _next_outbox_user_id[0] += 1
    uid = _next_outbox_user_id[0]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash) "
            "VALUES (%s, 'local', %s, %s, 'x')",
            (uid, f"outbox-test-{uid}@example.com", f"outbox-test-{uid}@example.com"),
        )
        cur.execute(
            "INSERT INTO anime (id, title_romaji) VALUES (%s, %s)",
            (uid, f"Outbox Test Anime {uid}"),
        )
    return uid


@pytest.mark.parametrize("source", ["ui_bulk_edit", "crunchyroll", "netflix", "plex", "primevideo"])
def test_enqueue_outbox_update_accepts_every_real_call_site_source(pg_conn, monkeypatch, source):
    uid = _make_outbox_user(pg_conn)
    monkeypatch.setattr(common, "USER_ID", uid)

    conn = psycopg2.connect(DATABASE_URL)
    try:
        common.enqueue_outbox_update(conn, uid, source, status="WATCHING")
        conn.commit()
    finally:
        conn.close()

    with pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT source FROM status_sync_outbox WHERE user_id = %s AND anime_id = %s",
            (uid, uid),
        )
        row = cur.fetchone()
    assert row["source"] == source


def test_enqueue_outbox_update_rejects_unknown_source(pg_conn, monkeypatch):
    uid = _make_outbox_user(pg_conn)
    monkeypatch.setattr(common, "USER_ID", uid)

    conn = psycopg2.connect(DATABASE_URL)
    try:
        with pytest.raises(psycopg2.errors.CheckViolation):
            common.enqueue_outbox_update(conn, uid, "not_a_real_provider", status="WATCHING")
    finally:
        conn.rollback()
        conn.close()
