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

    update_sql, update_params = conn.queries[0]
    assert "status = %s" in update_sql
    assert "progress = %s" not in update_sql
    assert "repeat_count = %s" not in update_sql
    assert "sync_status = 'pending'" in update_sql
    assert list(update_params) == ["COMPLETED", common.USER_ID, 42]

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
    assert list(update_params) == [5, 2, common.USER_ID, 42]

    insert_sql, insert_params = conn.queries[2]
    assert insert_params == (common.USER_ID, 42, "crunchyroll", None, 5, 2)


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
