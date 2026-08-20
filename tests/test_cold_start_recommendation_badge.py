"""
Coverage for issue #178 — cold-start seasonal recommendations (empty taste profile,
scored 0.00 with a fully-null `reason` by score_and_store()) were rendering a literal
"0%" badge in recommendations.html, reading as a broken app rather than the honest
"we don't know your taste yet, here's what's airing" story the seasonal digest
(issue #13) is meant to tell.

Two layers:
  1. _is_uninformative_reason() — pure function, no I/O. Detects a `reason` dict with
     no genre/tag/studio match and no cross-user corroboration.
  2. GET /recommendations rendered through the app's real Jinja2Templates instance via
     TestClient, per this repo's standing lesson (CLAUDE.local.md's "Dependency PR
     review process") that a route/template being merged on the strength of "it
     imports" or "returned 200" has caused a real production outage before — the only
     way to trust a template change is to actually render it and inspect the output.
     No Postgres needed: get_current_user, config.get, db.fetchall (used inline in the
     route for the "similar to" genre index), and _fetch_visible_recommendations are
     all monkeypatched to canned in-memory data, same shape a real Postgres row would
     produce (RealDictCursor rows behave like plain dicts).
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")


# ---------------------------------------------------------------------------
# _is_uninformative_reason() — pure function, no I/O
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def m():
    os.environ.setdefault("SESSION_SECRET_KEY", "test-key")
    import app.main as main_module
    return main_module


def _null_reason():
    return {
        "matched_genres": [],
        "matched_tags": [],
        "matched_studio": None,
        "cross_user_count": None,
        "cross_user_min_score": None,
    }


def test_fully_null_reason_is_uninformative(m):
    assert m._is_uninformative_reason(_null_reason()) is True


def test_none_reason_is_uninformative(m):
    assert m._is_uninformative_reason(None) is True


def test_empty_dict_reason_is_uninformative(m):
    assert m._is_uninformative_reason({}) is True


def test_matched_genres_makes_it_informative(m):
    reason = _null_reason()
    reason["matched_genres"] = ["Action"]
    assert m._is_uninformative_reason(reason) is False


def test_matched_tags_makes_it_informative(m):
    reason = _null_reason()
    reason["matched_tags"] = ["Time Travel"]
    assert m._is_uninformative_reason(reason) is False


def test_matched_studio_makes_it_informative(m):
    reason = _null_reason()
    reason["matched_studio"] = "MAPPA"
    assert m._is_uninformative_reason(reason) is False


def test_cross_user_count_makes_it_informative(m):
    reason = _null_reason()
    reason["cross_user_count"] = 2
    assert m._is_uninformative_reason(reason) is False


def test_zero_cross_user_count_is_still_uninformative(m):
    # A candidate that briefly had cross-user data land on 0 must not be treated as
    # informative just because the key is present — 0 other raters is no signal.
    reason = _null_reason()
    reason["cross_user_count"] = 0
    assert m._is_uninformative_reason(reason) is True


# ---------------------------------------------------------------------------
# GET /recommendations — real Jinja2 render via TestClient, no Postgres needed
# ---------------------------------------------------------------------------

COLD_START_SEASONAL_ID = 901   # uninformative reason + season_label present
COLD_START_NO_SEASON_ID = 902  # uninformative reason, no season_label (edge case)
REAL_MATCH_ID = 903            # genuine genre match — must be unaffected


def _cold_start_seasonal_row():
    return {
        "id": COLD_START_SEASONAL_ID,
        "title_english": None,
        "title_romaji": "Cold Start Seasonal Pick",
        "cover_image_url": None,
        "format": "TV",
        "episodes": 12,
        "average_score": None,
        "genres": ["Action"],
        "season": "FALL",
        "season_year": 2026,
        "rec_score": 0.0,
        "reason": _null_reason(),
        "source": "seasonal",
    }


def _cold_start_no_season_row():
    return {
        "id": COLD_START_NO_SEASON_ID,
        "title_english": None,
        "title_romaji": "Cold Start No Season",
        "cover_image_url": None,
        "format": "TV",
        "episodes": 12,
        "average_score": None,
        "genres": [],
        "season": None,
        "season_year": None,
        "rec_score": 0.0,
        "reason": {},
        "source": "similarity",
    }


def _real_match_row():
    return {
        "id": REAL_MATCH_ID,
        "title_english": "Real Match Show",
        "title_romaji": "Real Match Show",
        "cover_image_url": None,
        "format": "TV",
        "episodes": 24,
        "average_score": 78,
        "genres": ["Comedy"],
        "season": None,
        "season_year": None,
        "rec_score": 72.3,
        "reason": {
            "matched_genres": ["Comedy"],
            "matched_tags": [],
            "matched_studio": None,
            "cross_user_count": None,
            "cross_user_min_score": None,
        },
        "source": "similarity",
    }


@pytest.fixture()
def app_client(monkeypatch):
    """TestClient wired entirely to in-memory fakes — no Postgres. Not entered as a
    `with` block, so the app's startup event (APScheduler + instance_config reads)
    never runs, same pattern implicitly relied on by tests/test_sessions.py's
    app_client fixture."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as main_module
    from fastapi.testclient import TestClient

    fake_user = {"id": 1, "email": "test@example.com", "is_admin": False}
    monkeypatch.setattr(main_module, "get_current_user", lambda request: fake_user)
    monkeypatch.setattr(main_module.config, "get", lambda user_id, key: main_module.config.DEFAULTS.get(key, ""))
    # The route's own inline query (completed-library genres, for "similar to") —
    # not the thing under test here, so just return no completed history.
    monkeypatch.setattr(main_module.db, "fetchall", lambda *a, **k: [])

    return TestClient(main_module.app), main_module


def _card_html(body: str, anime_id: int) -> str:
    """Slice out just one rec-card's HTML block for isolated assertions, so a
    percentage or badge belonging to a different card can't produce a false pass."""
    marker = f'data-anime-id="{anime_id}"'
    start = body.index(marker)
    # Back up to the start of this card's opening <div, forward to the next card
    # (or end of the grid) — cards are emitted in id order matching our fixture rows.
    card_start = body.rindex('<div class="rec-card"', 0, start)
    next_card = body.find('<div class="rec-card"', start)
    return body[card_start:next_card] if next_card != -1 else body[card_start:]


def test_cold_start_seasonal_card_has_no_percent_badge(app_client, monkeypatch):
    client, m = app_client
    monkeypatch.setattr(
        m, "_fetch_visible_recommendations", lambda user_id: [_cold_start_seasonal_row()]
    )

    resp = client.get("/recommendations")
    assert resp.status_code == 200
    card = _card_html(resp.text, COLD_START_SEASONAL_ID)

    assert '0%' not in card
    assert 'rec-score-badge">' not in card  # no numeric badge div at all
    assert 'rec-score-badge--neutral' not in card  # season badge already covers it
    assert 'rec-season-badge' in card
    assert 'Fall 2026' in card


def test_cold_start_no_season_card_gets_neutral_badge_not_zero_percent(app_client, monkeypatch):
    client, m = app_client
    monkeypatch.setattr(
        m, "_fetch_visible_recommendations", lambda user_id: [_cold_start_no_season_row()]
    )

    resp = client.get("/recommendations")
    assert resp.status_code == 200
    card = _card_html(resp.text, COLD_START_NO_SEASON_ID)

    assert '0%' not in card
    assert 'rec-score-badge--neutral' in card
    assert 'rec-season-badge' not in card


def test_real_match_card_still_shows_numeric_badge(app_client, monkeypatch):
    client, m = app_client
    monkeypatch.setattr(
        m, "_fetch_visible_recommendations", lambda user_id: [_real_match_row()]
    )

    resp = client.get("/recommendations")
    assert resp.status_code == 200
    card = _card_html(resp.text, REAL_MATCH_ID)

    assert '72%' in card
    assert 'rec-score-badge--neutral' not in card
    assert 'Comedy' in card  # matched_genres still rendered as before


def test_mixed_batch_renders_each_card_correctly(app_client, monkeypatch):
    client, m = app_client
    monkeypatch.setattr(
        m,
        "_fetch_visible_recommendations",
        lambda user_id: [_cold_start_seasonal_row(), _cold_start_no_season_row(), _real_match_row()],
    )

    resp = client.get("/recommendations")
    assert resp.status_code == 200
    body = resp.text

    # Sanity: this is a real render, not a stub — actual translated page content
    # is present (per CLAUDE.local.md's "test the right route" lesson).
    assert 'rec-grid' in body

    seasonal_card = _card_html(body, COLD_START_SEASONAL_ID)
    no_season_card = _card_html(body, COLD_START_NO_SEASON_ID)
    real_card = _card_html(body, REAL_MATCH_ID)

    assert '0%' not in seasonal_card
    assert '0%' not in no_season_card
    assert 'rec-score-badge">' not in seasonal_card
    assert 'rec-score-badge--neutral' in no_season_card
    assert '72%' in real_card
