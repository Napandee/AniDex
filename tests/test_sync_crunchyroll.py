"""
Regression coverage for sync_crunchyroll.py's process() state machine, added
alongside the Netflix sync work (#48) specifically to guard the #47 shared-module
extraction: process() itself wasn't touched by that refactor, but nothing was
verifying it stays that way as the shared module keeps evolving for Netflix/Prime.
"""

import sync_crunchyroll as cr


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
