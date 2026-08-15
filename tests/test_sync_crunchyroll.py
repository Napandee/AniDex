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
    assert result["Attack on Titan"]["episode"] == 1
    assert result["Attack on Titan"]["watched_at"] == "2026-08-14T00:00:00Z"


def test_parse_items_skips_items_with_zero_episode_number():
    zero_ep = _episode_item(episode_number=0)
    assert cr.parse_items([zero_ep]) == {}


def test_parse_items_skips_items_with_no_title():
    untitled = {"date_played": "2026-08-14T00:00:00Z", "panel": {"episode_metadata": {"episode_number": 1}}}
    assert cr.parse_items([untitled]) == {}


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
    items = client.fetch_since(watermark)

    assert len(items) == 2  # only the two page-1 items — page 2's first item hits the watermark
    assert calls == [1, 2]  # never walks as far as page 3


def test_fetch_since_no_watermark_walks_until_short_page(monkeypatch):
    client = cr.CrunchyrollHistory("dummy-etp-rt")
    full_page = [_episode_item() for _ in range(cr.PAGE_SIZE)]
    short_page = [_episode_item()]
    pages = {1: full_page, 2: short_page}
    monkeypatch.setattr(client, "_fetch_page", lambda page: pages.get(page, []))

    items = client.fetch_since(None)

    assert len(items) == cr.PAGE_SIZE + 1


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
    monkeypatch.setattr(cr, "anilist_update", lambda anilist_id, **kw: calls.append(("update", anilist_id, kw)))
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


def test_repeating_branch_does_not_save_state_if_update_fails(monkeypatch):
    # Regression test (issue #52) — the identical bug shape was confirmed live in
    # sync_netflix.py's equivalent branch (issue #48): a 429 from AniList mid-write
    # left state saved as if the rewatch was handled while the real progress
    # update never landed, permanently hiding the miss from future watermark-based
    # syncs. anilist_update() must run before save_cr_state(), not after.
    calls = []
    monkeypatch.setattr(cr, "anilist_update", lambda anilist_id, **kw: (_ for _ in ()).throw(RuntimeError("429")))
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
