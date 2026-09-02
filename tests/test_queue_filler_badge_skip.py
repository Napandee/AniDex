"""
Coverage for issue #301 — a queue-card filler-status badge plus a "skip to next
canon" action, sourced read-only from #299's filler_episode_cache
(anime_id, episode_number, status IN ('canon', 'filler', 'mixed'), UNIQUE on
(anime_id, episode_number); absence of a row means "unknown", never "canon").

Two layers under test:

  1. next_episode_filler_info() — the pure, DB-free walk-logic helper in
     app/main.py, unit-tested directly (same pattern as rewatch_due(), see
     test_rewatch_queue.py's docstring for why this repo prefers testing logic
     directly over standing up Postgres). This is where the acceptance-criteria
     boundary cases live: a multi-episode filler run must be walked past in
     full, and an uncached (unknown) episode must stop the walk rather than
     being assumed to still be filler.

  2. The /queue route + queue.html render — real Jinja2 render via TestClient,
     db.fetchall monkeypatched to canned rows, same pattern as
     test_queue_paused_planning_move.py — proving the badge/action actually
     reach the page (and stay absent when they shouldn't), not just that the
     underlying logic is correct in isolation.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.main as main  # noqa: E402


# ── Layer 1: next_episode_filler_info() pure-logic tests ──────────────────────


def test_next_episode_unknown_status_no_badge_no_skip():
    # No cached row at all for episode 5 (progress=4) -- badge must stay silent
    # and there's nothing to skip.
    next_ep, status, target = main.next_episode_filler_info(4, {})
    assert next_ep == 5
    assert status is None
    assert target is None


def test_next_episode_canon_shows_badge_no_skip_action():
    next_ep, status, target = main.next_episode_filler_info(4, {5: "canon"})
    assert next_ep == 5
    assert status == "canon"
    assert target is None


def test_next_episode_mixed_shows_badge_no_skip_action():
    next_ep, status, target = main.next_episode_filler_info(4, {5: "mixed"})
    assert next_ep == 5
    assert status == "mixed"
    assert target is None


def test_single_filler_episode_skips_past_just_that_one():
    # Ep 5 filler, ep 6 canon -- skip target lands progress on 5 (episode 5
    # treated as skipped), leaving 6 (canon) as the new next-unwatched.
    filler_map = {5: "filler", 6: "canon"}
    next_ep, status, target = main.next_episode_filler_info(4, filler_map)
    assert next_ep == 5
    assert status == "filler"
    assert target == 5


def test_multi_episode_filler_run_walks_past_the_whole_run():
    # Acceptance criterion: a filler run of more than one episode must be
    # walked past in full, not just the first episode of the run. Eps 5-7
    # filler, ep 8 canon -- target should land on 7, not 5.
    filler_map = {5: "filler", 6: "filler", 7: "filler", 8: "canon"}
    next_ep, status, target = main.next_episode_filler_info(4, filler_map)
    assert next_ep == 5
    assert status == "filler"
    assert target == 7


def test_filler_run_stops_at_mixed_not_just_canon():
    filler_map = {5: "filler", 6: "filler", 7: "mixed"}
    next_ep, status, target = main.next_episode_filler_info(4, filler_map)
    assert target == 6


def test_filler_run_stops_at_uncached_unknown_episode_not_assumed_filler():
    # Acceptance criterion: the walk must stop at the first *uncached* episode
    # rather than assuming the run continues past what's actually been
    # researched -- even though episode 8 (never cached) is followed by another
    # cached filler episode 9, the walk must not skip past the unknown gap.
    filler_map = {5: "filler", 6: "filler", 8: "filler"}  # note: no key for 7
    next_ep, status, target = main.next_episode_filler_info(4, filler_map)
    assert next_ep == 5
    assert status == "filler"
    # Run is 5, 6 -- episode 7 has no cached row, so the walk stops there and
    # the skip target is 6 (episode 7 stays the next-unwatched, unresolved).
    assert target == 6


def test_progress_none_treated_as_zero():
    filler_map = {1: "filler", 2: "canon"}
    next_ep, status, target = main.next_episode_filler_info(None, filler_map)
    assert next_ep == 1
    assert status == "filler"
    assert target == 1


def test_entire_remaining_series_filler_walks_to_the_end_of_cached_data():
    filler_map = {5: "filler", 6: "filler", 7: "filler"}
    next_ep, status, target = main.next_episode_filler_info(4, filler_map)
    # No canon/mixed/unknown-stop within the cached range provided -- walk
    # runs off the end of what's cached (episode 8 has no row => unknown =>
    # stop), landing target on the last cached filler episode.
    assert target == 7


# ── Layer 2: /queue route + template render ────────────────────────────────

ANIME_SINGLE_FILLER = 921
ANIME_MULTI_FILLER = 922
ANIME_UNKNOWN_GAP = 923
ANIME_CANON_NEXT = 924
ANIME_NO_CACHE = 925


def _row(anime_id, progress=4):
    return {
        "id": anime_id,
        "title_english": None,
        "title_romaji": f"Filler Item {anime_id}",
        "cover_image_url": None,
        "format": "TV",
        "episodes": 24,
        "genres": ["Action"],
        "external_links": [],
        "average_score": 70,
        "status": "PLANNING",
        "progress": progress,
        "repeat_count": 0,
        "watch_next_priority": None,
        "personal_tags": [],
        "mood_tags": [],
        "notes": None,
        "rec_score": None,
        "rec_reason": None,
    }


def _filler_row(anime_id, episode_number, status):
    return {"anime_id": anime_id, "episode_number": episode_number, "status": status}


@pytest.fixture()
def app_client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    from fastapi.testclient import TestClient

    fake_user = {"id": 1, "email": "test@example.com", "is_admin": False}
    monkeypatch.setattr(main, "get_current_user", lambda request: fake_user)
    monkeypatch.setattr(main.config, "get", lambda user_id, key: main.config.DEFAULTS.get(key, ""))

    entries_rows = [
        _row(ANIME_SINGLE_FILLER),
        _row(ANIME_MULTI_FILLER),
        _row(ANIME_UNKNOWN_GAP),
        _row(ANIME_CANON_NEXT),
        _row(ANIME_NO_CACHE),
    ]
    filler_rows = [
        # Single-episode filler run: ep5 filler, ep6 canon.
        _filler_row(ANIME_SINGLE_FILLER, 5, "filler"),
        _filler_row(ANIME_SINGLE_FILLER, 6, "canon"),
        # Multi-episode filler run: ep5-7 filler, ep8 canon.
        _filler_row(ANIME_MULTI_FILLER, 5, "filler"),
        _filler_row(ANIME_MULTI_FILLER, 6, "filler"),
        _filler_row(ANIME_MULTI_FILLER, 7, "filler"),
        _filler_row(ANIME_MULTI_FILLER, 8, "canon"),
        # Unknown gap: ep5-6 filler, ep7 uncached, ep8 filler (must not be
        # reached by the walk).
        _filler_row(ANIME_UNKNOWN_GAP, 5, "filler"),
        _filler_row(ANIME_UNKNOWN_GAP, 6, "filler"),
        _filler_row(ANIME_UNKNOWN_GAP, 8, "filler"),
        # Next episode already canon -- badge only, no action.
        _filler_row(ANIME_CANON_NEXT, 5, "canon"),
        # ANIME_NO_CACHE has no rows at all -- neither badge nor action.
    ]

    call_count = {"n": 0}

    def fake_fetchall(*args, **kwargs):
        call_count["n"] += 1
        slot = (call_count["n"] - 1) % 3
        if slot == 0:
            return entries_rows
        if slot == 1:
            return filler_rows
        return []  # rewatch-reminder query, not under test here

    monkeypatch.setattr(main.db, "fetchall", fake_fetchall)

    return TestClient(main.app), main


def _item_html(body: str, anime_id: int) -> str:
    marker = f'data-anime-id="{anime_id}"'
    start = body.index(marker)
    item_start = body.rindex('<li class="list-row queue-item"', 0, start)
    next_item = body.find('<li class="list-row queue-item"', start)
    end = next_item if next_item != -1 else body.index("</ol>", start)
    return body[item_start:end]


def test_single_filler_run_shows_badge_and_skip_action(app_client):
    client, _m = app_client
    resp = client.get("/queue")
    assert resp.status_code == 200
    card = _item_html(resp.text, ANIME_SINGLE_FILLER)
    assert "badge-filler" in card
    assert "Ep 5" in card
    assert "btn-skip-filler" in card
    assert f'data-anime-id="{ANIME_SINGLE_FILLER}"' in card.split("btn-skip-filler")[1][:200]
    assert 'data-target-progress="5"' in card


def test_multi_episode_filler_run_skip_target_lands_past_whole_run(app_client):
    """Acceptance criterion: the skip action must advance past the entire
    contiguous filler run (eps 5-7), not just the first filler episode."""
    client, _m = app_client
    resp = client.get("/queue")
    card = _item_html(resp.text, ANIME_MULTI_FILLER)
    assert "btn-skip-filler" in card
    assert 'data-target-progress="7"' in card
    assert "badge-filler" in card


def test_unknown_episode_stops_the_walk_before_reaching_later_filler(app_client):
    """Acceptance criterion: an uncached (unknown) episode inside what would
    otherwise look like a longer filler run must stop the walk right there --
    the skip target must not jump past episode 7 (uncached) to reach episode 8
    (cached filler), since episode 7's status was never actually researched."""
    client, _m = app_client
    resp = client.get("/queue")
    card = _item_html(resp.text, ANIME_UNKNOWN_GAP)
    assert "btn-skip-filler" in card
    assert 'data-target-progress="6"' in card
    assert 'data-target-progress="8"' not in card


def test_next_episode_canon_shows_badge_but_no_skip_action(app_client):
    client, _m = app_client
    resp = client.get("/queue")
    card = _item_html(resp.text, ANIME_CANON_NEXT)
    assert "badge-filler" in card
    assert "btn-skip-filler" not in card


def test_no_cached_data_shows_neither_badge_nor_action(app_client):
    client, _m = app_client
    resp = client.get("/queue")
    card = _item_html(resp.text, ANIME_NO_CACHE)
    assert "badge-filler" not in card
    assert "btn-skip-filler" not in card


def test_skip_action_reuses_existing_progress_endpoint_no_new_route():
    """Guardrail check: the only AniList-write endpoints this repo allows are
    rating/status/progress. Confirm no new route was registered for this
    feature -- app/static/script.js's btn-skip-filler handler must POST to the
    pre-existing /api/anime/{id}/progress path, not a new one."""
    script_js = Path(__file__).resolve().parent.parent / "app" / "static" / "script.js"
    text = script_js.read_text()
    handler = text.split("btn-skip-filler")[1]
    assert "/api/anime/${animeId}/progress" in handler[:1500]
