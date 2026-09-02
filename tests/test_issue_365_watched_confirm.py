"""
Coverage for issue #365 — the queue "Watched" button used to jump straight from
whatever progress a title was at (including 0) to full episode count + COMPLETED
status in one click, with no confirmation. A misclick on an unstarted, long-running
Planning title silently completed it and pushed that state to AniList.

script.js now gates the click behind a confirm() whenever more than one episode
would be skipped (btn.dataset.progress vs btn.dataset.episodes) — a title already on
its last episode still completes with one click, since "Watched" and "mark complete"
are the same action there.

This file can only verify the template-side wiring (the button carries the right
data-progress/data-episodes so script.js has correct numbers to gate on) — there's
no JS test runner in this repo, so the confirm() gating logic itself needs the usual
live-browser check before merge, same as any other script.js change here.

Real Jinja2 render via TestClient, no Postgres needed — db.fetchall is monkeypatched
to canned rows, same pattern as test_queue_paused_planning_move.py.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

UNSTARTED_ID = 921  # progress 0, 24 episodes — the exact misclick scenario from #365
ALMOST_DONE_ID = 922  # progress 23 of 24 — one episode left, should stay frictionless
NO_EPISODE_COUNT_ID = 923  # episodes unknown (ongoing/unconfirmed) — data-episodes empty


def _row(anime_id, progress, episodes):
    return {
        "id": anime_id,
        "title_english": None,
        "title_romaji": f"Queue Item {anime_id}",
        "cover_image_url": None,
        "format": "TV",
        "episodes": episodes,
        "genres": ["Action"],
        "external_links": [],
        "average_score": 70,
        "status": "PLANNING" if progress == 0 else "WATCHING",
        "progress": progress,
        "repeat_count": 0,
        "watch_next_priority": None,
        "personal_tags": [],
        "mood_tags": [],
        "notes": None,
        "rec_score": None,
        "rec_reason": None,
    }


@pytest.fixture()
def app_client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as main_module
    from fastapi.testclient import TestClient

    fake_user = {"id": 1, "email": "test@example.com", "is_admin": False}
    monkeypatch.setattr(main_module, "get_current_user", lambda request: fake_user)
    monkeypatch.setattr(main_module.config, "get", lambda user_id, key: main_module.config.DEFAULTS.get(key, ""))

    call_count = {"n": 0}
    rows = [
        _row(UNSTARTED_ID, 0, 24),
        _row(ALMOST_DONE_ID, 23, 24),
        _row(NO_EPISODE_COUNT_ID, 5, None),
    ]

    def fake_fetchall(*args, **kwargs):
        call_count["n"] += 1
        if (call_count["n"] - 1) % 3 == 0:
            return rows
        return []

    monkeypatch.setattr(main_module.db, "fetchall", fake_fetchall)

    return TestClient(main_module.app), main_module


def _item_html(body: str, anime_id: int) -> str:
    marker = f'data-anime-id="{anime_id}"'
    start = body.index(marker)
    item_start = body.rindex('<li class="list-row queue-item"', 0, start)
    next_item = body.find('<li class="list-row queue-item"', start)
    end = next_item if next_item != -1 else body.index('</ol>', start)
    return body[item_start:end]


def test_watched_button_carries_current_progress(app_client):
    client, _m = app_client
    resp = client.get("/queue")
    card = _item_html(resp.text, UNSTARTED_ID)
    assert 'data-progress="0"' in card
    assert 'data-episodes="24"' in card


def test_watched_button_progress_defaults_to_zero_when_none(app_client):
    """personal_notes/library_entries.progress can be NULL for a never-touched
    entry — the template must render data-progress="0" (Jinja `or 0`), not
    data-progress="" or data-progress="None", or script.js's parseInt would
    produce NaN and the >1-remaining comparison would silently misbehave."""
    client, _m = app_client
    resp = client.get("/queue")
    card = _item_html(resp.text, ALMOST_DONE_ID)
    assert 'data-progress="23"' in card
    assert 'data-episodes="24"' in card


def test_watched_button_handles_unknown_episode_count(app_client):
    """episodes=None (ongoing/unconfirmed show) already rendered data-episodes=""
    before this issue — confirm that's untouched, since script.js's `if (episodes)`
    guard means no confirm is possible (and no progress-only PATCH) when the total
    is unknown, same as before #365."""
    client, _m = app_client
    resp = client.get("/queue")
    card = _item_html(resp.text, NO_EPISODE_COUNT_ID)
    assert 'data-episodes=""' in card
    assert 'data-progress="5"' in card
