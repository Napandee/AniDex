from datetime import datetime, timezone

import sync_netflix as nf

# ── Parsing helpers ──────────────────────────────────────────────────────────
# Fixture shapes match sync_netflix.py's current assumptions (viewedItems / date /
# seriesTitle / title / episode), credited to statsoflife/extract-netflix-activity's
# documented shape but NOT yet confirmed against a live Netflix response — see
# probe_netflix_shakti.py. If a live capture shows different field names, update
# both the parsing helpers in sync_netflix.py and the fixtures below together, so
# this suite keeps testing what the parser actually assumes.

EPISODE_ITEM = {
    "date": 1723600000000,  # 2024-08-14T00:26:40Z
    "seriesTitle": "Attack on Titan",
    "title": "Attack on Titan: Season 1: Episode 5",
    "episode": 5,
}

MOVIE_ITEM = {
    "date": 1723600000000,
    "title": "Your Name.",
}


def test_item_watched_at_parses_epoch_milliseconds():
    dt = nf._item_watched_at(EPISODE_ITEM)
    assert dt == datetime.fromtimestamp(EPISODE_ITEM["date"] / 1000, tz=timezone.utc)


def test_item_watched_at_missing_date_returns_none():
    assert nf._item_watched_at({"title": "no date"}) is None


def test_is_episode_true_when_series_title_present():
    assert nf._is_episode(EPISODE_ITEM) is True


def test_is_episode_false_for_movie_item():
    assert nf._is_episode(MOVIE_ITEM) is False


def test_item_episode_number_from_structured_field():
    assert nf._item_episode_number(EPISODE_ITEM) == 5


def test_item_episode_number_falls_back_to_title_parsing():
    item = {"title": "Some Show: Season 2: Episode 12"}
    assert nf._item_episode_number(item) == 12


def test_item_episode_number_defaults_to_zero_when_unparseable():
    assert nf._item_episode_number({"title": "no episode info here"}) == 0


def test_aggregate_by_series_picks_most_recently_watched_per_title():
    older = {**EPISODE_ITEM, "date": 1723500000000, "episode": 3}
    newer = {**EPISODE_ITEM, "date": 1723600000000, "episode": 5}
    result = nf.aggregate_by_series([older, newer])
    assert result["Attack on Titan"]["episode"] == 5
    assert result["Attack on Titan"]["watched_format"] == "TV"


def test_aggregate_by_series_tags_movies_correctly():
    result = nf.aggregate_by_series([MOVIE_ITEM])
    assert result["Your Name."]["watched_format"] == "MOVIE"
    assert result["Your Name."]["episode"] == 1


# ── process() state machine ──────────────────────────────────────────────────
# Mirrors sync_crunchyroll.py's process() branches, adapted to a watermark instead
# of a remembered episode number. Every case here represents "genuinely new
# activity" per fetch_since()'s contract — there's no separate "no episode
# progress" fixture because the caller (main()) never invokes process() for
# unchanged series in the first place.

def _watched(fmt="TV", episode=5, watched_at=None):
    return {
        "watched_format": fmt,
        "episode": episode,
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
    result = nf.process("Your Name.", _watched(fmt="MOVIE", episode=1), entry, None, conn=None)
    assert "COMPLETED" in result
    assert ("update", 42, {"progress": 1, "status": "COMPLETED"}) in calls


def test_movie_rewatch_increments_repeat(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="COMPLETED", progress=1, repeat=0, total=1)
    result = nf.process("Your Name.", _watched(fmt="MOVIE", episode=1), entry, None, conn=None)
    assert "rewatch" in result
    assert ("update", 42, {"repeat": 1}) in calls


def test_progress_advances_for_current_series(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="CURRENT", progress=3, total=24)
    result = nf.process("Attack on Titan", _watched(episode=5), entry, None, conn=None)
    assert "progress 3 → 5" in result
    assert ("update", 42, {"progress": 5}) in calls


def test_dropped_series_resumes_to_current(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="DROPPED", progress=3, total=24)
    result = nf.process("Attack on Titan", _watched(episode=5), entry, None, conn=None)
    assert "resumed" in result
    assert ("update", 42, {"progress": 5, "status": "CURRENT"}) in calls


def test_already_repeating_status_detected_and_advanced(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=2, repeat=1, total=24)
    result = nf.process("Attack on Titan", _watched(episode=4), entry, None, conn=None)
    assert "rewatch detected" in result
    assert ("update", 42, {"progress": 4}) in calls
    assert ("save", 42, True) in calls


def test_completed_series_watched_from_lower_episode_starts_rewatch(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="COMPLETED", progress=24, repeat=0, total=24)
    # A baseline must already exist (nf_state not None) — process() treats a
    # COMPLETED series with no prior state as an unverifiable first sighting,
    # not a rewatch, since it can't yet tell "first sync" from "rewatch".
    nf_state = {"last_seen_watched_at": None, "rewatch_in_progress": False}
    result = nf.process("Attack on Titan", _watched(episode=1), entry, nf_state, conn=None)
    assert "rewatch started" in result
    assert ("update", 42, {"progress": 1, "status": "REPEATING"}) in calls


def test_already_at_or_ahead_makes_no_anilist_call(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="CURRENT", progress=10, total=24)
    result = nf.process("Attack on Titan", _watched(episode=5), entry, None, conn=None)
    assert "already at or ahead" in result
    assert not any(c[0] == "update" for c in calls)
    assert ("save", 42, False) in calls
