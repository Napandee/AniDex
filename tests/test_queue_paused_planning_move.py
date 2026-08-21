"""
Coverage for issue #259 — two /queue usability fixes:

  1. The notes/detail-edit link on each queue card was a bare Unicode pencil
     glyph (queue-notes-link) that rendered as a barely-visible mark in practice.
     Fixed by (a) styling it as a legible .btn-notes-sm chip with a text label
     instead of a lone glyph, and (b) making the whole card clickable through to
     the notes page (script.js click-delegation on .queue-item, mirroring the
     .card delegation already used on library.html), with data-notes-back on the
     <li> carrying the back= param the old per-link href used to encode.
  2. Paused-status queue cards had no quick way back to Planning. Fixed by a new
     btn-move-planning button, rendered only for e.status == 'PAUSED', reusing
     the existing POST /api/anime/{id}/status endpoint with {status: 'PLANNING'}
     exactly as btn-mark-watched already does with {status: 'COMPLETED'}.

Real Jinja2 render via TestClient, no Postgres needed — db.fetchall is
monkeypatched to canned rows, same pattern as
test_cold_start_recommendation_badge.py (see that file's docstring for why a
real render, not just "returned 200", is this repo's bar for trusting a
template change).
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PLANNING_ID = 911
PAUSED_ID = 912


def _row(anime_id, status):
    return {
        "id": anime_id,
        "title_english": None,
        "title_romaji": f"Queue Item {anime_id}",
        "cover_image_url": None,
        "format": "TV",
        "episodes": 12,
        "genres": ["Action"],
        "external_links": [],
        "average_score": 70,
        "status": status,
        "progress": 3,
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
    """TestClient wired to in-memory fakes — no Postgres, mirrors
    test_cold_start_recommendation_badge.py's app_client fixture. Not entered as
    a `with` block so the app's startup event never runs."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as main_module
    from fastapi.testclient import TestClient

    fake_user = {"id": 1, "email": "test@example.com", "is_admin": False}
    monkeypatch.setattr(main_module, "get_current_user", lambda request: fake_user)
    monkeypatch.setattr(main_module.config, "get", lambda user_id, key: main_module.config.DEFAULTS.get(key, ""))

    call_count = {"n": 0}

    def fake_fetchall(*args, **kwargs):
        call_count["n"] += 1
        # queue() makes exactly two db.fetchall calls per request, in order:
        # (1) the PLANNING/PAUSED entries query, (2) the rewatch-reminder
        # completed_rows query (not under test here) — so odd calls are always
        # the first of a pair, regardless of how many requests this fixture
        # instance serves across a single test.
        if call_count["n"] % 2 == 1:
            return [_row(PLANNING_ID, "PLANNING"), _row(PAUSED_ID, "PAUSED")]
        return []

    monkeypatch.setattr(main_module.db, "fetchall", fake_fetchall)

    return TestClient(main_module.app), main_module


def _item_html(body: str, anime_id: int) -> str:
    """Slice out just one queue card's <li>...</li> block for isolated
    assertions, so a button belonging to a different card can't produce a false
    pass. Cards are <li class="queue-item" ... data-anime-id="{id}" ...>."""
    marker = f'data-anime-id="{anime_id}"'
    start = body.index(marker)
    item_start = body.rindex('<li class="queue-item"', 0, start)
    next_item = body.find('<li class="queue-item"', start)
    end = next_item if next_item != -1 else body.index('</ol>', start)
    return body[item_start:end]


def test_queue_renders_both_entries(app_client):
    client, _m = app_client
    resp = client.get("/queue")
    assert resp.status_code == 200
    assert f'data-anime-id="{PLANNING_ID}"' in resp.text
    assert f'data-anime-id="{PAUSED_ID}"' in resp.text


def test_planning_card_has_no_move_to_planning_button(app_client):
    client, _m = app_client
    resp = client.get("/queue")
    card = _item_html(resp.text, PLANNING_ID)
    assert "btn-move-planning" not in card


def test_paused_card_has_move_to_planning_button_targeting_right_id(app_client):
    client, _m = app_client
    resp = client.get("/queue")
    card = _item_html(resp.text, PAUSED_ID)
    assert "btn-move-planning" in card
    # The button's own data-anime-id must match this card's anime, not just be
    # present somewhere in the card's HTML.
    move_btn_start = card.index("btn-move-planning")
    move_btn_tag = card[max(0, move_btn_start - 200):move_btn_start + 50]
    assert f'data-anime-id="{PAUSED_ID}"' in move_btn_tag


def test_both_tabs_get_move_button_on_paused_only(app_client):
    """Acceptance criterion: works consistently on All, Planning, and Paused
    tabs. The mocked rows are identical regardless of ?status=, since the real
    per-tab SQL filtering is untouched by this issue — this asserts the
    per-card conditional (e.status == 'PAUSED') holds across each tab render."""
    client, _m = app_client
    for query in ("", "?status=PLANNING", "?status=PAUSED"):
        resp = client.get(f"/queue{query}")
        assert resp.status_code == 200
        planning_card = _item_html(resp.text, PLANNING_ID)
        paused_card = _item_html(resp.text, PAUSED_ID)
        assert "btn-move-planning" not in planning_card
        assert "btn-move-planning" in paused_card


def test_notes_link_is_a_legible_chip_not_a_bare_glyph(app_client):
    client, _m = app_client
    resp = client.get("/queue")
    assert "queue-notes-link" not in resp.text  # old barely-visible class, retired
    card = _item_html(resp.text, PLANNING_ID)
    assert "btn-notes-sm" in card
    assert f"/anime/{PLANNING_ID}/notes?back=PLANNING" in card


def test_queue_item_carries_notes_back_for_card_click_through(app_client):
    """The click-delegation in script.js reads item.dataset.notesBack — the <li>
    must actually carry data-notes-back for that to resolve to anything but the
    'PLANNING' JS fallback."""
    client, _m = app_client
    resp = client.get("/queue")
    planning_card = _item_html(resp.text, PLANNING_ID)
    paused_card = _item_html(resp.text, PAUSED_ID)
    assert 'data-notes-back="PLANNING"' in planning_card
    assert 'data-notes-back="PLANNING"' in paused_card


def test_move_to_planning_button_label_uses_i18n_key(app_client):
    client, _m = app_client
    resp = client.get("/queue")
    card = _item_html(resp.text, PAUSED_ID)
    assert "Move to Planning" in card
