"""
Regression coverage for sync_plex.py (issue #153). process() is a near-verbatim
port of sync_crunchyroll.py's process() — see that module's docstring for the
full branch-by-branch reasoning (including the #328 rewatch-clamp fix) — since
Plex, like Crunchyroll, reports real absolute episode numbers rather than
Netflix's delta-only counts. These tests mirror tests/test_sync_crunchyroll.py's
process() coverage 1:1 so the two can't silently drift, plus parse_items()
coverage adapted to Plex's actual history-item field names
(grandparentTitle/index/parentIndex/viewedAt/type — see
notes/2026-08-19-plex-sync-research.md).
"""

from datetime import datetime, timezone

import sync_plex as plex

# ── Parsing helpers ──────────────────────────────────────────────────────────
# Field names per notes/2026-08-19-plex-sync-research.md's source-confirmed field
# list for GET /status/sessions/history/all (grandparentTitle/index/parentIndex
# are standard Plex video-object fields, not endpoint-specific — see that note's
# "CONFIDENCE NOTE" on what's live-verified vs. read from source).

def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


EPISODE_ITEM = {
    "type": "episode",
    "grandparentTitle": "Attack on Titan",
    "title": "Episode Title",
    "parentIndex": 1,
    "index": 5,
    "viewedAt": _ts("2026-08-14T20:00:00Z"),
}


def _episode_item(**overrides):
    item = {**EPISODE_ITEM}
    item.update(overrides)
    return item


def test_parse_items_picks_most_recently_watched_episode_not_highest():
    # A rewatch: ep 12 watched weeks ago, ep 1 watched yesterday — process() needs
    # the ep-1 position to detect the rewatch, not the historically-highest episode.
    older = _episode_item(viewedAt=_ts("2026-08-01T00:00:00Z"), index=12)
    newer = _episode_item(viewedAt=_ts("2026-08-14T00:00:00Z"), index=1)
    result = plex.parse_items([older, newer])
    assert result[("Attack on Titan", 1)]["episode"] == 1
    assert result[("Attack on Titan", 1)]["watched_at"] == datetime(2026, 8, 14, tzinfo=timezone.utc)


def test_parse_items_skips_items_with_zero_episode_number():
    zero_ep = _episode_item(index=0)
    assert plex.parse_items([zero_ep]) == {}


def test_parse_items_skips_items_with_no_title():
    untitled = {"type": "episode", "index": 1, "viewedAt": _ts("2026-08-14T00:00:00Z")}
    assert plex.parse_items([untitled]) == {}


def test_parse_items_skips_items_with_no_viewed_at():
    no_timestamp = _episode_item(viewedAt=None)
    assert plex.parse_items([no_timestamp]) == {}


def test_parse_items_keeps_two_seasons_of_same_franchise_separate():
    season1 = _episode_item(
        grandparentTitle="Youjo Senki", parentIndex=1, index=12,
        viewedAt=_ts("2026-08-10T00:00:00Z"),
    )
    season2 = _episode_item(
        grandparentTitle="Youjo Senki", parentIndex=2, index=3,
        viewedAt=_ts("2026-08-14T00:00:00Z"),
    )
    result = plex.parse_items([season1, season2])
    assert set(result.keys()) == {("Youjo Senki", 1), ("Youjo Senki", 2)}
    assert result[("Youjo Senki", 1)]["episode"] == 12
    assert result[("Youjo Senki", 2)]["episode"] == 3


def test_parse_items_defaults_season_to_1_when_missing_or_invalid():
    no_season = {
        "type": "episode", "grandparentTitle": "Frieren", "index": 5,
        "viewedAt": _ts("2026-08-14T00:00:00Z"),
    }
    bad_season = _episode_item(grandparentTitle="Frieren", parentIndex="not-a-number", index=5)
    assert list(plex.parse_items([no_season]).keys()) == [("Frieren", 1)]
    assert list(plex.parse_items([bad_season]).keys()) == [("Frieren", 1)]


def test_parse_items_treats_season_zero_as_season_1():
    item = _episode_item(grandparentTitle="Frieren", parentIndex=0, index=5)
    assert list(plex.parse_items([item]).keys()) == [("Frieren", 1)]


def test_parse_items_movie_uses_title_not_grandparent_title():
    movie = {
        "type": "movie", "title": "Weathering With You",
        "viewedAt": _ts("2026-08-14T00:00:00Z"),
    }
    result = plex.parse_items([movie])
    assert set(result.keys()) == {("Weathering With You", 1)}
    assert result[("Weathering With You", 1)]["episode"] == 1
    assert result[("Weathering With You", 1)]["watched_format"] == "MOVIE"


def test_compute_fetch_watermark_returns_max_across_series():
    state = {
        1: {"last_seen_watched_at": datetime(2026, 8, 1, tzinfo=timezone.utc)},
        2: {"last_seen_watched_at": datetime(2026, 8, 10, tzinfo=timezone.utc)},
    }
    assert plex.compute_fetch_watermark(state) == datetime(2026, 8, 10, tzinfo=timezone.utc)


def test_compute_fetch_watermark_none_when_no_state_recorded():
    assert plex.compute_fetch_watermark({}) is None
    assert plex.compute_fetch_watermark({1: {"last_seen_watched_at": None}}) is None


# ── process() — ported 1:1 from test_sync_crunchyroll.py ────────────────────

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
    monkeypatch.setattr(plex, "_update", lambda conn, anilist_id, **kw: calls.append(("update", anilist_id, kw)))
    monkeypatch.setattr(
        plex, "save_plex_state",
        lambda conn, anilist_id, title, last_ep, rewatch: calls.append(("save", anilist_id, last_ep, rewatch)),
    )
    return calls


def test_progress_advances_for_current_series(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="CURRENT", progress=3, total=24)
    result = plex.process("Attack on Titan", plex_ep=5, entry=entry, plex_state=None, conn=None)
    assert "progress 3 → 5" in result
    assert ("update", 42, {"progress": 5}) in calls


def test_dropped_series_resumes_to_current(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="DROPPED", progress=3, total=24)
    state = {"last_seen_episode": 3, "rewatch_in_progress": False}
    result = plex.process("Attack on Titan", plex_ep=5, entry=entry, plex_state=state, conn=None)
    assert "resumed" in result
    assert ("update", 42, {"progress": 5, "status": "CURRENT"}) in calls


def test_already_repeating_status_detected_and_advanced(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=2, repeat=1, total=24)
    result = plex.process("Attack on Titan", plex_ep=4, entry=entry, plex_state=None, conn=None)
    assert "rewatch detected" in result
    assert ("update", 42, {"progress": 4}) in calls


def test_completed_series_dropping_below_last_seen_starts_rewatch(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="COMPLETED", progress=24, repeat=0, total=24)
    state = {"last_seen_episode": 24, "rewatch_in_progress": False}
    result = plex.process("Attack on Titan", plex_ep=1, entry=entry, plex_state=state, conn=None)
    assert "rewatch started" in result
    assert ("update", 42, {"progress": 1, "status": "REPEATING"}) in calls


def test_rewatch_completion_increments_repeat(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=20, repeat=1, total=24)
    state = {"last_seen_episode": 20, "rewatch_in_progress": True}
    result = plex.process("Attack on Titan", plex_ep=24, entry=entry, plex_state=state, conn=None)
    assert "rewatch complete" in result
    assert ("update", 42, {"progress": 24, "status": "COMPLETED", "repeat": 2}) in calls


def test_first_sighting_of_completed_series_records_baseline_without_update(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="COMPLETED", progress=24, repeat=0, total=24)
    result = plex.process("Attack on Titan", plex_ep=24, entry=entry, plex_state=None, conn=None)
    assert "first-sync" in result
    assert not any(c[0] == "update" for c in calls)


def test_no_progress_since_last_sync_makes_no_anilist_call(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="CURRENT", progress=5, total=24)
    state = {"last_seen_episode": 5, "rewatch_in_progress": False}
    result = plex.process("Attack on Titan", plex_ep=5, entry=entry, plex_state=state, conn=None)
    assert "no change" in result
    assert not any(c[0] == "update" for c in calls)


# ── Same #328-shaped rewatch-clamp coverage as Crunchyroll ───────────────────

def test_new_rewatch_pass_restarting_from_a_low_episode_is_detected(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=10, repeat=0, total=13)
    state = {"last_seen_episode": 10, "rewatch_in_progress": True}
    result = plex.process("Alderamin on the Sky", plex_ep=6, entry=entry, plex_state=state, conn=None)
    assert "new rewatch pass detected" in result
    assert ("update", 42, {"progress": 6}) in calls
    assert ("save", 42, 6, True) in calls


def test_new_rewatch_pass_detection_works_several_passes_deep(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=8, repeat=3, total=12)
    state = {"last_seen_episode": 8, "rewatch_in_progress": True}
    result = plex.process("The Kingdoms of Ruin", plex_ep=2, entry=entry, plex_state=state, conn=None)
    assert "new rewatch pass detected" in result
    assert ("update", 42, {"progress": 2}) in calls
    assert ("save", 42, 2, True) in calls


def test_pre_fix_behavior_would_have_silently_clamped_to_the_old_peak(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=10, repeat=0, total=13)
    state = {"last_seen_episode": 10, "rewatch_in_progress": True}
    plex.process("Alderamin on the Sky", plex_ep=6, entry=entry, plex_state=state, conn=None)
    save_calls = [c for c in calls if c[0] == "save"]
    assert len(save_calls) == 1
    assert save_calls[0][2] == 6, "must store the new pass's real episode, not the old high-water mark"


def test_equal_episode_while_rewatching_does_not_falsely_trigger_new_pass_detection(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=6, repeat=0, total=13)
    state = {"last_seen_episode": 6, "rewatch_in_progress": True}
    result = plex.process("Alderamin on the Sky", plex_ep=6, entry=entry, plex_state=state, conn=None)
    assert "new rewatch pass detected" not in result
    assert not any(c[0] == "update" for c in calls)


def test_new_entry_creates_watching_status_at_detected_episode(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status=None, progress=0, repeat=0, total=None)
    result = plex.process("The Testament of Sister New Devil", plex_ep=3, entry=entry, plex_state=None, conn=None)
    assert "new" in result.lower()
    assert ("update", 42, {"progress": 3, "status": "WATCHING"}) in calls
    assert ("save", 42, 3, False) in calls


def test_new_entry_branch_checked_before_every_other_branch(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status=None, progress=0, repeat=0, total=12)
    result = plex.process("Some New Show", plex_ep=12, entry=entry, plex_state=None, conn=None)
    assert ("update", 42, {"progress": 12, "status": "WATCHING"}) in calls
    assert "COMPLETED" not in result
    assert "rewatch" not in result.lower()


# ── DRY_RUN mode (issue #387, Part 2) — this script had none before; every
# conn-taking write function must safely no-op with conn=None, and the reads
# must return an empty/from-scratch shape, matching sync_netflix.py's existing
# DRY_RUN precedent exactly.

def test_load_title_search_cache_empty_when_conn_is_none():
    assert plex.load_title_search_cache(None) == {}


def test_save_title_search_cache_entry_noop_when_conn_is_none():
    plex.save_title_search_cache_entry(None, "Some Western Show", None)  # must not raise


def test_save_plex_state_noop_when_conn_is_none():
    plex.save_plex_state(None, 154587, "Attack on Titan", 5, False)  # must not raise


def test_save_watermark_noop_when_conn_is_none():
    plex.save_watermark(None, 154587, "Attack on Titan", datetime.now(timezone.utc))  # must not raise


def test_update_logs_instead_of_enqueueing_when_dry_run(monkeypatch):
    monkeypatch.setattr(plex, "DRY_RUN", True)
    called = []
    monkeypatch.setattr(plex, "enqueue_outbox_update", lambda *a, **kw: called.append((a, kw)))
    plex._update(None, 154587, status="WATCHING", progress=3)
    assert called == []


def test_save_state_logs_instead_of_saving_when_dry_run(monkeypatch):
    monkeypatch.setattr(plex, "DRY_RUN", True)
    called = []
    monkeypatch.setattr(plex, "save_plex_state", lambda *a, **kw: called.append((a, kw)))
    plex._save_state(None, 154587, "Attack on Titan", 5, False)
    assert called == []


def test_update_and_save_state_call_through_when_not_dry_run(monkeypatch):
    assert plex.DRY_RUN is False
    enqueue_called = []
    monkeypatch.setattr(plex, "enqueue_outbox_update", lambda *a, **kw: enqueue_called.append((a, kw)))
    plex._update(object(), 154587, status="WATCHING")
    assert len(enqueue_called) == 1

    save_called = []
    monkeypatch.setattr(plex, "save_plex_state", lambda *a, **kw: save_called.append((a, kw)))
    plex._save_state(object(), 154587, "Attack on Titan", 5, False)
    assert len(save_called) == 1


# ── Guid-based fast path (issue #447) ────────────────────────────────────────
# Prefix formats confirmed by reading HAMA/MyAnimeList.bundle source — see
# sync_plex.py's module docstring for exactly how.

def test_parse_agent_guid_ids_finds_hama_anidb_id():
    guids = ["com.plexapp.agents.hama://anidb-4776", "imdb://tt0121220"]
    assert plex.parse_agent_guid_ids(guids) == (4776, None)


def test_parse_agent_guid_ids_finds_myanimelist_bundle_mal_id():
    guids = ["net.fribbtastic.coding.plex.myanimelist://16498", "tmdb://1429"]
    assert plex.parse_agent_guid_ids(guids) == (None, 16498)


def test_parse_agent_guid_ids_finds_both_when_present():
    guids = [
        "com.plexapp.agents.hama://anidb-4776",
        "net.fribbtastic.coding.plex.myanimelist://16498",
    ]
    assert plex.parse_agent_guid_ids(guids) == (4776, 16498)


def test_parse_agent_guid_ids_none_when_only_default_agent_guids_present():
    guids = ["imdb://tt2560140", "tmdb://tv/32281", "tvdb://267440"]
    assert plex.parse_agent_guid_ids(guids) == (None, None)


def test_parse_agent_guid_ids_ignores_malformed_guid_strings():
    assert plex.parse_agent_guid_ids(["not-a-guid-at-all"]) == (None, None)


def test_resolve_anilist_id_from_guids_prefers_mal_when_both_present():
    mapping = {"anidb": {4776: 999}, "mal": {16498: 16498}}
    guids = [
        "com.plexapp.agents.hama://anidb-4776",
        "net.fribbtastic.coding.plex.myanimelist://16498",
    ]
    assert plex.resolve_anilist_id_from_guids(guids, mapping) == 16498


def test_resolve_anilist_id_from_guids_falls_back_to_anidb():
    mapping = {"anidb": {4776: 999}, "mal": {}}
    guids = ["com.plexapp.agents.hama://anidb-4776"]
    assert plex.resolve_anilist_id_from_guids(guids, mapping) == 999


def test_resolve_anilist_id_from_guids_none_when_id_not_in_mapping():
    mapping = {"anidb": {}, "mal": {}}
    guids = ["com.plexapp.agents.hama://anidb-4776"]
    assert plex.resolve_anilist_id_from_guids(guids, mapping) is None


def test_resolve_anilist_id_from_guids_none_for_default_agent_guids():
    mapping = {"anidb": {4776: 999}, "mal": {16498: 16498}}
    guids = ["imdb://tt2560140", "tmdb://tv/32281"]
    assert plex.resolve_anilist_id_from_guids(guids, mapping) is None


def test_fetch_item_guids_parses_metadata_response(monkeypatch):
    client = plex.PlexHistory("https://plex.example", "token")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "MediaContainer": {
                    "Metadata": [
                        {
                            "Guid": [
                                {"id": "com.plexapp.agents.hama://anidb-4776"},
                                {"id": "imdb://tt0121220"},
                            ]
                        }
                    ]
                }
            }

    monkeypatch.setattr(client.client, "get", lambda *a, **kw: _Resp())
    guids = client.fetch_item_guids("12345")
    assert guids == ["com.plexapp.agents.hama://anidb-4776", "imdb://tt0121220"]


def test_fetch_item_guids_empty_on_request_failure(monkeypatch):
    client = plex.PlexHistory("https://plex.example", "token")

    def _raise(*a, **kw):
        raise ConnectionError("boom")

    monkeypatch.setattr(client.client, "get", _raise)
    assert client.fetch_item_guids("12345") == []


def test_fetch_item_guids_empty_when_no_metadata_returned(monkeypatch):
    client = plex.PlexHistory("https://plex.example", "token")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"MediaContainer": {}}

    monkeypatch.setattr(client.client, "get", lambda *a, **kw: _Resp())
    assert client.fetch_item_guids("12345") == []


def test_parse_items_captures_grandparent_rating_key_for_episodes():
    item = _episode_item(grandparentRatingKey="789")
    result = plex.parse_items([item])
    assert result[("Attack on Titan", 1)]["rating_key"] == "789"


def test_parse_items_captures_rating_key_for_movies():
    item = {
        "type": "movie",
        "title": "A Silent Voice",
        "ratingKey": "555",
        "viewedAt": _ts("2026-08-14T20:00:00Z"),
    }
    result = plex.parse_items([item])
    assert result[("A Silent Voice", 1)]["rating_key"] == "555"


def test_load_id_mapping_cache_empty_when_conn_is_none():
    assert plex.load_id_mapping_cache(None) == {"anidb": {}, "mal": {}}
