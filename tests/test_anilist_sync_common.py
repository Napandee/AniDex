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


# ── resolve_or_create_user_list_entry — issue #252 ──────────────────────────
# The real bug: incremental CR/Netflix sync resolved a title correctly (e.g. via
# the title-search cache) but the anime wasn't on the user's AniList list, so it
# was silently skipped forever. Reproduces issue #252's Context section shape:
# a title resolves to a media_id with no existing user_list/library_entries row.

def test_full_pull_unmatched_title_still_skips():
    # Regression coverage for the deliberately-unchanged case: the initial
    # full-history walk (or a user-triggered Force Full Resync, #20/#21) must
    # NOT auto-create — same behavior as before this issue.
    user_list = {}
    conn = _FakeOutboxConn()
    decision = common.resolve_or_create_user_list_entry(
        20678, "The Testament of Sister New Devil", user_list, full_pull=True, conn=conn,
    )
    assert decision == "skip"
    assert user_list == {}
    assert conn.queries == []  # no anime stub written


def test_incremental_unmatched_title_creates_synthetic_entry():
    # The fix: a normal day-to-day incremental sync (full_pull=False) for a title
    # that resolved correctly but isn't tracked yet creates a new entry instead.
    user_list = {}
    conn = _FakeOutboxConn()
    decision = common.resolve_or_create_user_list_entry(
        20678, "The Testament of Sister New Devil", user_list, full_pull=False, conn=conn,
    )
    assert decision == "create"
    assert user_list[20678] == {
        "status": None, "progress": 0, "repeat": 0,
        "total_episodes": None, "format": None,
        "title": "The Testament of Sister New Devil",
    }
    # The anime stub is written before the caller's own outbox enqueue, so the
    # library_entries/status_sync_outbox FK constraint on anime_id is satisfied.
    sql, params = conn.queries[0]
    assert "INSERT INTO anime" in sql
    assert params == (20678, "The Testament of Sister New Devil")


def test_incremental_unmatched_title_dry_run_skips_db_write():
    # sync_netflix.py's DRY_RUN mode passes conn=None — no DB write, but the
    # synthetic entry is still added so the rest of DRY_RUN's logging/process()
    # path exercises the same decision a real run would make.
    user_list = {}
    decision = common.resolve_or_create_user_list_entry(
        20678, "The Testament of Sister New Devil", user_list, full_pull=False, conn=None,
    )
    assert decision == "create"
    assert user_list[20678]["status"] is None


def test_already_tracked_title_returns_existing_and_does_not_touch_user_list():
    user_list = {154587: {"status": "CURRENT", "progress": 3, "repeat": 0,
                           "total_episodes": 24, "format": "TV", "title": "Attack on Titan"}}
    conn = _FakeOutboxConn()
    decision = common.resolve_or_create_user_list_entry(
        154587, "Attack on Titan", user_list, full_pull=False, conn=conn,
    )
    assert decision == "existing"
    assert user_list[154587]["status"] == "CURRENT"  # untouched
    assert conn.queries == []


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
