"""
Regression coverage for sync_crunchyroll.py's process() state machine, added
alongside the Netflix sync work (#48) specifically to guard the #47 shared-module
extraction: process() itself wasn't touched by that refactor, but nothing was
verifying it stays that way as the shared module keeps evolving for Netflix/Prime.

Coverage for the fetch/watermark layer added for issue #45 (direct CR API fetch
replacing crunchyexporter-cli) lives in this same file, below — process() itself
is untouched by that change, and these new tests exist specifically to make sure
it stays that way.
"""

from datetime import datetime, timezone

import sync_crunchyroll as cr

# ── Parsing helpers ──────────────────────────────────────────────────────────
# Fixture shape matches the vendored crunchyexporter-cli's CRHistory._parse_item
# (src/crunchyroll/history.py, pinned commit 1855e567ad1704a6655feedffcf76b1d77e5d690)
# — the real content/v2/{account_id}/watch-history item shape. Ordering (newest-first)
# is verified separately, live, via scripts/dev/probe-crunchyroll.sh.

EPISODE_ITEM = {
    "date_played": "2026-08-14T20:00:00Z",
    "fully_watched": True,
    "panel": {
        "id": "EP123",
        "title": "Episode Title",
        "episode_metadata": {
            "series_id": "S1",
            "series_title": "Attack on Titan",
            "season_number": 1,
            "episode_number": 5,
        },
    },
}


def _episode_item(**overrides):
    item = {**EPISODE_ITEM, "panel": {**EPISODE_ITEM["panel"], "episode_metadata": {**EPISODE_ITEM["panel"]["episode_metadata"]}}}
    for key in ("date_played", "fully_watched"):
        if key in overrides:
            item[key] = overrides.pop(key)
    item["panel"]["episode_metadata"].update(overrides)
    return item


def test_parse_watched_at_handles_z_suffix():
    dt = cr._parse_watched_at("2026-08-14T20:00:00Z")
    assert dt == datetime(2026, 8, 14, 20, 0, 0, tzinfo=timezone.utc)


def test_parse_watched_at_none_for_missing_or_empty():
    assert cr._parse_watched_at(None) is None
    assert cr._parse_watched_at("") is None


def test_parse_watched_at_none_for_unparseable():
    assert cr._parse_watched_at("not-a-date") is None


def test_parse_items_picks_most_recently_watched_episode_not_highest():
    # A rewatch: ep 12 watched weeks ago, ep 1 watched yesterday — process() needs
    # the ep-1 position to detect the rewatch, not the historically-highest episode.
    older = _episode_item(date_played="2026-08-01T00:00:00Z", episode_number=12)
    newer = _episode_item(date_played="2026-08-14T00:00:00Z", episode_number=1)
    result = cr.parse_items([older, newer])
    assert result[("Attack on Titan", 1)]["episode"] == 1
    assert result[("Attack on Titan", 1)]["watched_at"] == "2026-08-14T00:00:00Z"


def test_parse_items_skips_items_with_zero_episode_number():
    zero_ep = _episode_item(episode_number=0)
    assert cr.parse_items([zero_ep]) == {}


def test_parse_items_skips_items_with_no_title():
    untitled = {"date_played": "2026-08-14T00:00:00Z", "panel": {"episode_metadata": {"episode_number": 1}}}
    assert cr.parse_items([untitled]) == {}


# ── Season-aware keying (issue #159) ─────────────────────────────────────────

def test_parse_items_keeps_two_seasons_of_same_franchise_separate():
    # The core #159 bug: watching Saga of Tanya the Evil S2 in the same sync window
    # as S1 activity must not collapse into a single dict entry that only keeps
    # whichever has the most recent date_played — both seasons' progress needs to
    # survive so both get written to their respective AniList entries.
    season1 = _episode_item(
        series_title="Youjo Senki", season_number=1, episode_number=12,
        date_played="2026-08-10T00:00:00Z",
    )
    season2 = _episode_item(
        series_title="Youjo Senki", season_number=2, episode_number=3,
        date_played="2026-08-14T00:00:00Z",
    )
    result = cr.parse_items([season1, season2])
    assert set(result.keys()) == {("Youjo Senki", 1), ("Youjo Senki", 2)}
    assert result[("Youjo Senki", 1)]["episode"] == 12
    assert result[("Youjo Senki", 2)]["episode"] == 3


def test_parse_items_defaults_season_to_1_when_missing_or_invalid():
    no_season = {
        "date_played": "2026-08-14T00:00:00Z",
        "panel": {"episode_metadata": {"series_title": "Frieren", "episode_number": 5}},
    }
    bad_season = _episode_item(series_title="Frieren", season_number="not-a-number", episode_number=5)
    assert cr.parse_items([no_season]) == {("Frieren", 1): {"episode": 5, "watched_at": "2026-08-14T00:00:00Z"}}
    assert cr.parse_items([bad_season]) == {("Frieren", 1): {"episode": 5, "watched_at": "2026-08-14T20:00:00Z"}}


def test_parse_items_treats_season_zero_as_season_1():
    # CR occasionally reports season_number 0 for specials/OVAs bundled with a main
    # series — treat that the same as "no season info" rather than as a distinct key.
    item = _episode_item(series_title="Frieren", season_number=0, episode_number=5)
    assert list(cr.parse_items([item]).keys()) == [("Frieren", 1)]


# ── Manual title/season overrides (issue #159) ───────────────────────────────
class _FakeOverrideConn:
    def __init__(self, rows=None):
        self._rows = rows or []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        pass

    def fetchall(self):
        return self._rows


def test_load_title_overrides_keys_by_lowercased_title_and_season(monkeypatch):
    monkeypatch.setattr(cr, "USER_ID", 1)
    conn = _FakeOverrideConn(rows=[
        {"series_title": "Kingdom", "season_number": 2, "anilist_id": 999},
        {"series_title": "Youjo Senki", "season_number": 1, "anilist_id": 100},
    ])
    result = cr.load_title_overrides(conn)
    assert result == {("kingdom", 2): 999, ("youjo senki", 1): 100}


def test_load_title_overrides_empty_when_no_rows(monkeypatch):
    monkeypatch.setattr(cr, "USER_ID", 1)
    assert cr.load_title_overrides(_FakeOverrideConn()) == {}


# ── resolve_media_id — override/heuristic priority (issue #159) ─────────────

def test_resolve_media_id_override_wins_without_touching_anilist(monkeypatch):
    # Acceptance criterion: once an override is set, a sync uses it without
    # re-matching — no AniList search call should happen at all.
    def fail_if_called(*a, **kw):
        raise AssertionError("find_anilist_id should not be called when an override exists")

    monkeypatch.setattr(cr, "find_anilist_id", fail_if_called)
    overrides = {("kingdom", 2): 999}
    result = cr.resolve_media_id("Kingdom", 2, overrides, title_index={"kingdom": 1})
    assert result == {
        "media_id": 999, "matched_via_override": True,
        "in_index_before": False, "bare_title_in_index_before": False,
        "via_season_suffix": False,
    }


def test_resolve_media_id_override_is_season_specific():
    # An override for season 2 must not apply to a season-1 (or season-3) entry of
    # the same franchise.
    overrides = {("kingdom", 2): 999}
    title_index = {"kingdom": 1}
    result = cr.resolve_media_id("Kingdom", 1, overrides, title_index)
    assert result["matched_via_override"] is False
    assert result["media_id"] == 1


def test_resolve_media_id_falls_through_to_heuristic_when_no_override(monkeypatch):
    monkeypatch.setattr(cr, "find_anilist_id", lambda title, idx, season_number=1: 2)
    result = cr.resolve_media_id("Kingdom", 2, overrides={}, title_index={"kingdom": 1})
    assert result["matched_via_override"] is False
    assert result["media_id"] == 2


def test_resolve_media_id_flags_season_suffix_match_for_cache_guard(monkeypatch):
    # Simulates find_anilist_id resolving via a season-suffix candidate: it would
    # have populated title_index at the suffixed key, not the bare title's.
    def fake_find(title, title_index, season_number=1):
        title_index["kingdom 2nd season"] = 2
        return 2

    monkeypatch.setattr(cr, "find_anilist_id", fake_find)
    result = cr.resolve_media_id("Kingdom", 2, overrides={}, title_index={})
    assert result["via_season_suffix"] is True
    assert result["media_id"] == 2


def test_resolve_media_id_bare_title_fallback_not_flagged_as_season_suffix(monkeypatch):
    # find_anilist_id resolving via the bare-title fallback (heuristic found
    # nothing) must NOT be flagged as a season-suffix match, so main()'s cache
    # persistence guard still lets it persist to the shared bare-title cache.
    def fake_find(title, title_index, season_number=1):
        title_index[title.lower()] = 1
        return 1

    monkeypatch.setattr(cr, "find_anilist_id", fake_find)
    result = cr.resolve_media_id("Kingdom", 2, overrides={}, title_index={})
    assert result["via_season_suffix"] is False
    assert result["media_id"] == 1


# ── Fetch-side watermark ──────────────────────────────────────────────────────

def test_compute_fetch_watermark_returns_max_across_series():
    state_map = {
        1: {"last_seen_watched_at": datetime(2026, 8, 1, tzinfo=timezone.utc)},
        2: {"last_seen_watched_at": datetime(2026, 8, 14, tzinfo=timezone.utc)},
    }
    assert cr.compute_fetch_watermark(state_map) == datetime(2026, 8, 14, tzinfo=timezone.utc)


def test_compute_fetch_watermark_none_when_no_state_recorded():
    assert cr.compute_fetch_watermark({}) is None
    assert cr.compute_fetch_watermark({1: {"last_seen_watched_at": None}}) is None


# ── Walk-completeness tracking (issue #97) ───────────────────────────────────
# Minimal in-memory stand-in for a psycopg2 connection — just enough to cover
# load_walk_complete/_set_walk_complete's single-key settings SQL, not a
# general-purpose DB fake.
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


def test_load_walk_complete_defaults_to_has_existing_state_when_never_set():
    # No settings row yet — an existing account with real sync state shouldn't
    # get hit with a surprise full re-walk the first time this ships.
    assert cr.load_walk_complete(_FakeWalkConn(initial_value=None), has_existing_state=True) is True
    # A genuinely first-ever sync (no prior state) starts as not-complete.
    assert cr.load_walk_complete(_FakeWalkConn(initial_value=None), has_existing_state=False) is False


def test_load_walk_complete_reads_explicit_stored_value():
    assert cr.load_walk_complete(_FakeWalkConn(initial_value="true"), has_existing_state=False) is True
    assert cr.load_walk_complete(_FakeWalkConn(initial_value="false"), has_existing_state=True) is False


def test_set_walk_complete_persists_and_commits():
    conn = _FakeWalkConn()
    cr._set_walk_complete(conn, True)
    assert conn.value == "true"
    assert conn.committed is True

    cr._set_walk_complete(conn, False)
    assert conn.value == "false"


# ── Persistent title-search cache (issue #115) ───────────────────────────────
# Minimal in-memory stand-in for a psycopg2 connection, just for
# load_title_search_cache/save_title_search_cache_entry's title->media_id SQL.
class _FakeSearchCacheConn:
    def __init__(self, initial=None):
        self.store = dict(initial or {})  # {title: media_id}
        self.committed = False
        self._rows = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        q = query.strip()
        if q.startswith("SELECT"):
            self._rows = [{"title": t, "media_id": m} for t, m in self.store.items()]
        elif q.startswith("INSERT"):
            title, media_id = params
            self.store[title] = media_id

    def fetchall(self):
        return self._rows

    def commit(self):
        self.committed = True


def test_load_title_search_cache_returns_all_rows():
    conn = _FakeSearchCacheConn(initial={"Frieren": 154587, "Some Western Show": None})
    assert cr.load_title_search_cache(conn) == {"Frieren": 154587, "Some Western Show": None}


def test_load_title_search_cache_empty_when_no_rows():
    assert cr.load_title_search_cache(_FakeSearchCacheConn()) == {}


def test_save_title_search_cache_entry_persists_and_commits():
    conn = _FakeSearchCacheConn()
    cr.save_title_search_cache_entry(conn, "Some Western Show", None)
    assert conn.store == {"Some Western Show": None}
    assert conn.committed is True

    cr.save_title_search_cache_entry(conn, "Frieren", 154587)
    assert conn.store == {"Some Western Show": None, "Frieren": 154587}


# ── CrunchyrollHistory.fetch_since — the actual incremental-fetch fix (#45) ───
# _fetch_page is monkeypatched per test so these never touch the network — same
# discipline as _capture()'s AniList/DB monkeypatching below.

def test_fetch_since_stops_at_watermark_without_including_it(monkeypatch):
    # A page shorter than PAGE_SIZE is itself treated as end-of-history, so shrink
    # PAGE_SIZE to fit this test's 2-item pages — otherwise page 1 alone would look
    # like a short/final page and page 2 would never even be requested.
    monkeypatch.setattr(cr, "PAGE_SIZE", 2)
    client = cr.CrunchyrollHistory("dummy-etp-rt")
    page1 = [
        _episode_item(date_played="2026-08-14T00:00:00Z"),
        _episode_item(date_played="2026-08-13T00:00:00Z"),
    ]
    page2 = [
        _episode_item(date_played="2026-08-10T00:00:00Z"),  # == watermark: excluded, stops here
        _episode_item(date_played="2026-08-09T00:00:00Z"),
    ]
    pages = {1: page1, 2: page2, 3: [_episode_item(date_played="2020-01-01T00:00:00Z")]}
    calls = []

    def fake_fetch_page(page):
        calls.append(page)
        return pages.get(page, [])

    monkeypatch.setattr(client, "_fetch_page", fake_fetch_page)
    watermark = datetime(2026, 8, 10, tzinfo=timezone.utc)
    items, reached_true_end = client.fetch_since(watermark)

    assert len(items) == 2  # only the two page-1 items — page 2's first item hits the watermark
    assert calls == [1, 2]  # never walks as far as page 3
    assert reached_true_end is False  # stopped at the watermark, not genuine end of history


def test_fetch_since_no_watermark_walks_until_short_page(monkeypatch):
    client = cr.CrunchyrollHistory("dummy-etp-rt")
    full_page = [_episode_item() for _ in range(cr.PAGE_SIZE)]
    short_page = [_episode_item()]
    pages = {1: full_page, 2: short_page}
    monkeypatch.setattr(client, "_fetch_page", lambda page: pages.get(page, []))

    items, reached_true_end = client.fetch_since(None)

    assert len(items) == cr.PAGE_SIZE + 1
    assert reached_true_end is True  # stopped because page 2 was short, i.e. genuine end of history


def _entry(status="CURRENT", progress=0, repeat=0, total=24, anilist_id=42):
    return {
        "status": status,
        "progress": progress,
        "repeat": repeat,
        "total_episodes": total,
        "anilist_id": anilist_id,
    }


def _capture(monkeypatch):
    calls = []
    monkeypatch.setattr(cr, "_update", lambda conn, anilist_id, **kw: calls.append(("update", anilist_id, kw)))
    monkeypatch.setattr(
        cr, "save_cr_state",
        lambda conn, anilist_id, title, last_ep, rewatch: calls.append(("save", anilist_id, last_ep, rewatch)),
    )
    return calls


def test_progress_advances_for_current_series(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="CURRENT", progress=3, total=24)
    result = cr.process("Attack on Titan", cr_ep=5, entry=entry, cr_state=None, conn=None)
    assert "progress 3 → 5" in result
    assert ("update", 42, {"progress": 5}) in calls


def test_dropped_series_resumes_to_current(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="DROPPED", progress=3, total=24)
    cr_state = {"last_seen_episode": 3, "rewatch_in_progress": False}
    result = cr.process("Attack on Titan", cr_ep=5, entry=entry, cr_state=cr_state, conn=None)
    assert "resumed" in result
    assert ("update", 42, {"progress": 5, "status": "CURRENT"}) in calls


def test_already_repeating_status_detected_and_advanced(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=2, repeat=1, total=24)
    result = cr.process("Attack on Titan", cr_ep=4, entry=entry, cr_state=None, conn=None)
    assert "rewatch detected" in result
    assert ("update", 42, {"progress": 4}) in calls


def test_completed_series_dropping_below_last_seen_starts_rewatch(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="COMPLETED", progress=24, repeat=0, total=24)
    cr_state = {"last_seen_episode": 24, "rewatch_in_progress": False}
    result = cr.process("Attack on Titan", cr_ep=1, entry=entry, cr_state=cr_state, conn=None)
    assert "rewatch started" in result
    assert ("update", 42, {"progress": 1, "status": "REPEATING"}) in calls


def test_rewatch_completion_increments_repeat(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=20, repeat=1, total=24)
    cr_state = {"last_seen_episode": 20, "rewatch_in_progress": True}
    result = cr.process("Attack on Titan", cr_ep=24, entry=entry, cr_state=cr_state, conn=None)
    assert "rewatch complete" in result
    assert ("update", 42, {"progress": 24, "status": "COMPLETED", "repeat": 2}) in calls


def test_first_sighting_of_completed_series_records_baseline_without_update(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="COMPLETED", progress=24, repeat=0, total=24)
    result = cr.process("Attack on Titan", cr_ep=24, entry=entry, cr_state=None, conn=None)
    assert "first-sync" in result
    assert not any(c[0] == "update" for c in calls)


def test_no_progress_since_last_sync_makes_no_anilist_call(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="CURRENT", progress=5, total=24)
    cr_state = {"last_seen_episode": 5, "rewatch_in_progress": False}
    result = cr.process("Attack on Titan", cr_ep=5, entry=entry, cr_state=cr_state, conn=None)
    assert "no change" in result
    assert not any(c[0] == "update" for c in calls)


def test_new_entry_creates_watching_status_at_detected_episode(monkeypatch):
    # Issue #252 — the resolved decision: a brand-new entry (status=None is
    # main()'s create sentinel, built by resolve_or_create_user_list_entry() for
    # an incremental sync's unmatched-title case) defaults to WATCHING at the
    # detected CR episode, not left implicit and not just a bare progress bump.
    calls = _capture(monkeypatch)
    entry = _entry(status=None, progress=0, repeat=0, total=None)
    result = cr.process("The Testament of Sister New Devil", cr_ep=3, entry=entry, cr_state=None, conn=None)
    assert "new" in result.lower()
    assert ("update", 42, {"progress": 3, "status": "WATCHING"}) in calls
    assert ("save", 42, 3, False) in calls


def test_new_entry_branch_checked_before_every_other_branch(monkeypatch):
    # status=None must win over every other check in process() — a real AniList
    # status is never None, so nothing else should ever match first regardless of
    # what cr_ep/total look like (e.g. cr_ep >= total, which would otherwise read
    # like a rewatch-completion for an existing REPEATING entry).
    calls = _capture(monkeypatch)
    entry = _entry(status=None, progress=0, repeat=0, total=12)
    result = cr.process("Some New Show", cr_ep=12, entry=entry, cr_state=None, conn=None)
    assert ("update", 42, {"progress": 12, "status": "WATCHING"}) in calls
    assert "COMPLETED" not in result
    assert "rewatch" not in result.lower()


def test_repeating_branch_does_not_save_state_if_update_fails(monkeypatch):
    # Regression test (issue #52) — the identical bug shape was confirmed live in
    # sync_netflix.py's equivalent branch (issue #48): a mid-write failure left
    # state saved as if the rewatch was handled while the real progress update
    # never landed, permanently hiding the miss from future watermark-based syncs.
    # _update() must run before save_cr_state(), not after — issue #100 changed
    # what _update() does (enqueues to the outbox instead of pushing to AniList
    # directly) but the ordering guarantee this test protects is unchanged: an
    # exception from _update() must still prevent save_cr_state() from running.
    calls = []
    monkeypatch.setattr(cr, "_update", lambda conn, anilist_id, **kw: (_ for _ in ()).throw(RuntimeError("db error")))
    monkeypatch.setattr(
        cr, "save_cr_state",
        lambda conn, anilist_id, title, last_ep, rewatch: calls.append(("save", anilist_id, last_ep, rewatch)),
    )
    entry = _entry(status="REPEATING", progress=2, repeat=1, total=24)
    try:
        cr.process("Attack on Titan", cr_ep=4, entry=entry, cr_state=None, conn=None)
    except RuntimeError:
        pass
    assert calls == []
