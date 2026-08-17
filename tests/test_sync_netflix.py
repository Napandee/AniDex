from datetime import datetime, timezone

import sync_netflix as nf

# ── Parsing helpers ──────────────────────────────────────────────────────────
# Fixture shapes match the real Falcor pathEvaluator response, confirmed live
# against a real account on feature/netflix-sync-48 (2026-08-14) — see
# scripts/dev/probe_netflix_falcor.py. Movie item shape is inferred (no
# `series` key observed in a real movie watch), not directly confirmed.

EPISODE_ITEM = {
    "movieID": 81554238,
    "date": 1771967200821,
    "dateStr": "24/2/26",
    "title": 'Season 1: "Fathers"',
    "episodeTitle": "Fathers",
    "seasonDescriptor": "Season 1",
    "series": 81450827,
    "seriesTitle": "The Night Agent",
}

MOVIE_ITEM = {
    "movieID": 70317773,
    "date": 1771967200821,
    "dateStr": "24/2/26",
    "title": "Your Name.",
}


def test_item_watched_at_parses_epoch_milliseconds():
    dt = nf._item_watched_at(EPISODE_ITEM)
    assert dt == datetime.fromtimestamp(EPISODE_ITEM["date"] / 1000, tz=timezone.utc)


def test_item_watched_at_missing_date_returns_none():
    assert nf._item_watched_at({"title": "no date"}) is None


def test_is_episode_true_when_series_key_present():
    assert nf._is_episode(EPISODE_ITEM) is True


def test_is_episode_false_for_movie_item():
    assert nf._is_episode(MOVIE_ITEM) is False


def test_aggregate_by_series_groups_raw_items_by_title():
    ep1 = {**EPISODE_ITEM, "movieID": 1, "date": 1000000}
    ep2 = {**EPISODE_ITEM, "movieID": 2, "date": 2000000}
    result = nf.aggregate_by_series([ep1, ep2])
    assert result["The Night Agent"]["watched_format"] == "TV"
    assert result["The Night Agent"]["items"] == [
        (1, nf._item_watched_at(ep1)),
        (2, nf._item_watched_at(ep2)),
    ]


def test_aggregate_by_series_tags_movies_correctly():
    result = nf.aggregate_by_series([MOVIE_ITEM])
    assert result["Your Name."]["watched_format"] == "MOVIE"
    assert result["Your Name."]["items"] == [
        (MOVIE_ITEM["movieID"], nf._item_watched_at(MOVIE_ITEM)),
    ]


# ── _new_episode_count() — the #19 fix ────────────────────────────────────────
# new_count must be computed against THIS series' own last_seen_watched_at, not
# just whatever the fetch happened to return, so it stays correct even when the
# fetch itself isn't watermark-bounded (a forced full re-fetch).

def test_new_episode_count_dedupes_by_movie_id():
    items = [
        (1, datetime(2026, 1, 1, tzinfo=timezone.utc)),
        (2, datetime(2026, 1, 2, tzinfo=timezone.utc)),
        (2, datetime(2026, 1, 2, tzinfo=timezone.utc)),  # e.g. pagination overlap
    ]
    count, watched_at = nf._new_episode_count(items, None)
    assert count == 2
    assert watched_at == datetime(2026, 1, 2, tzinfo=timezone.utc)


def test_new_episode_count_no_watermark_counts_everything():
    # First-ever sighting of a series (no stored state yet) — nothing to filter
    # against, so the full batch counts, same as the pre-fix behavior.
    items = [(i, datetime(2026, 1, i, tzinfo=timezone.utc)) for i in range(1, 13)]
    count, watched_at = nf._new_episode_count(items, None)
    assert count == 12


def test_new_episode_count_filters_against_per_series_watermark():
    # Normal incremental sync: watermark = this series' own last_seen_watched_at.
    watermark = datetime(2026, 1, 10, tzinfo=timezone.utc)
    items = [(i, datetime(2026, 1, i, tzinfo=timezone.utc)) for i in range(1, 13)]
    count, watched_at = nf._new_episode_count(items, watermark)
    assert count == 2  # only ep 11 and 12 are newer than the watermark
    assert watched_at == datetime(2026, 1, 12, tzinfo=timezone.utc)


def test_new_episode_count_forced_full_refetch_matches_incremental_result():
    # The actual #19 bug: simulating a forced full re-fetch (the whole 12-episode
    # history reappears, not just the tail) must produce the SAME new_count as a
    # normal incremental sync would have — not the whole historical count stacked
    # on top of AniList's already-correct progress.
    watermark = datetime(2026, 1, 10, tzinfo=timezone.utc)
    full_history = [(i, datetime(2026, 1, i, tzinfo=timezone.utc)) for i in range(1, 13)]
    incremental_tail = [(i, datetime(2026, 1, i, tzinfo=timezone.utc)) for i in range(11, 13)]

    full_count, _ = nf._new_episode_count(full_history, watermark)
    incremental_count, _ = nf._new_episode_count(incremental_tail, watermark)
    assert full_count == incremental_count == 2


def test_new_episode_count_nothing_new_returns_none_watermark():
    watermark = datetime(2026, 1, 10, tzinfo=timezone.utc)
    items = [(i, datetime(2026, 1, i, tzinfo=timezone.utc)) for i in range(1, 11)]
    count, watched_at = nf._new_episode_count(items, watermark)
    assert count == 0
    assert watched_at is None


def test_new_episode_count_falls_back_to_one_for_items_without_movie_id():
    items = [(None, datetime(2026, 1, 1, tzinfo=timezone.utc))]
    count, watched_at = nf._new_episode_count(items, None)
    assert count == 1


# ── process() state machine ──────────────────────────────────────────────────
# watched["episode"] is AniList's current progress + new_count (computed in
# main(), before process() is called) for the "continuing" cases — see
# process()'s own docstring for why the COMPLETED-rewatch-start case uses
# watched["new_count"] instead.

def _watched(fmt="TV", episode=5, new_count=2, watched_at=None):
    return {
        "watched_format": fmt,
        "episode": episode,
        "new_count": new_count,
        "watched_at": watched_at or datetime(2026, 8, 14, tzinfo=timezone.utc),
    }


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
    monkeypatch.setattr(nf, "_update", lambda anilist_id, **kw: calls.append(("update", anilist_id, kw)))
    monkeypatch.setattr(
        nf, "_save_state",
        lambda conn, anilist_id, title, watched_at, rewatch: calls.append(("save", anilist_id, rewatch)),
    )
    return calls


def test_movie_first_watch_marks_completed(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="PLANNING", progress=0, total=1)
    result = nf.process("Your Name.", _watched(fmt="MOVIE", episode=1, new_count=1), entry, None, conn=None)
    assert "COMPLETED" in result
    assert ("update", 42, {"progress": 1, "status": "COMPLETED"}) in calls


def test_movie_rewatch_increments_repeat(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="COMPLETED", progress=1, repeat=0, total=1)
    result = nf.process("Your Name.", _watched(fmt="MOVIE", episode=1, new_count=1), entry, None, conn=None)
    assert "rewatch" in result
    assert ("update", 42, {"repeat": 1}) in calls


def test_progress_advances_by_new_episode_count(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="CURRENT", progress=3, total=24)
    # 2 new episodes seen -> episode field (computed by main() as al_ep + new_count) = 5
    result = nf.process("The Night Agent", _watched(episode=5, new_count=2), entry, None, conn=None)
    assert "progress 3 → 5" in result
    assert ("update", 42, {"progress": 5}) in calls


def test_dropped_series_resumes_to_current(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="DROPPED", progress=3, total=24)
    result = nf.process("The Night Agent", _watched(episode=5, new_count=2), entry, None, conn=None)
    assert "resumed" in result
    assert ("update", 42, {"progress": 5, "status": "CURRENT"}) in calls


def test_already_repeating_status_detected_and_advanced(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=2, repeat=1, total=24)
    result = nf.process("The Night Agent", _watched(episode=4, new_count=2), entry, None, conn=None)
    assert "rewatch detected" in result
    assert ("update", 42, {"progress": 4}) in calls
    assert ("save", 42, True) in calls


def test_completed_series_with_new_activity_starts_fresh_rewatch(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="COMPLETED", progress=24, repeat=0, total=24)
    # A baseline must already exist (nf_state not None) — process() treats a
    # COMPLETED series with no prior state as an unverifiable first sighting,
    # not a rewatch (see the branch above this one).
    nf_state = {"last_seen_watched_at": None, "rewatch_in_progress": False}
    # episode = al_ep(24) + new_count(3) = 27 would be wrong for a rewatch —
    # process() must use new_count alone (3), not the continuing-case episode field.
    result = nf.process("The Night Agent", _watched(episode=27, new_count=3), entry, nf_state, conn=None)
    assert "rewatch started" in result
    assert ("update", 42, {"progress": 3, "status": "REPEATING"}) in calls


def test_already_at_or_ahead_makes_no_anilist_call(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="CURRENT", progress=10, total=24)
    result = nf.process("The Night Agent", _watched(episode=5, new_count=2), entry, None, conn=None)
    assert "already at or ahead" in result
    assert not any(c[0] == "update" for c in calls)
    assert ("save", 42, False) in calls


def test_repeating_branch_does_not_save_state_if_update_fails(monkeypatch):
    # Regression test — confirmed live 2026-08-14: a 429 from AniList mid-write in
    # this exact branch left rewatch_in_progress=true saved with the real progress
    # update never landed, permanently hiding the miss from future watermark-based
    # fetches. _update() must run before _save_state(), not after.
    calls = []
    monkeypatch.setattr(nf, "_update", lambda anilist_id, **kw: (_ for _ in ()).throw(RuntimeError("429")))
    monkeypatch.setattr(
        nf, "_save_state",
        lambda conn, anilist_id, title, watched_at, rewatch: calls.append(("save", anilist_id, rewatch)),
    )
    entry = _entry(status="REPEATING", progress=2, repeat=1, total=24)
    try:
        nf.process("The Night Agent", _watched(episode=4, new_count=2), entry, None, conn=None)
    except RuntimeError:
        pass
    assert calls == []
