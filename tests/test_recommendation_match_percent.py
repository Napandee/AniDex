"""
Coverage for issue #227 — Netflix-style "% match" display: a read-time min-max
normalization of `rec_score` across the recommendations currently visible on a
given page load, shown as a new chip next to the existing reason chips.
Deliberately independent of scripts/run_recommender.py's own write-time
max-only normalization that already produces the corner `.rec-score-badge`
value — see `_compute_match_percentages()`'s docstring in app/main.py for why
the two numbers are allowed to differ instead of being the same figure twice.

Two layers, mirroring tests/test_cold_start_recommendation_badge.py's pattern:
  1. _compute_match_percentages() — pure function, no I/O, mutates a list of
     entry dicts in place.
  2. GET /recommendations rendered through the app's real Jinja2Templates
     instance via TestClient (no Postgres), per this repo's standing
     "test the right route" lesson — a template change is only trustworthy
     once actually rendered and inspected.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")


@pytest.fixture(scope="module")
def m():
    os.environ.setdefault("SESSION_SECRET_KEY", "test-key")
    import app.main as main_module
    return main_module


def _entry(rec_score, uninformative=False):
    return {"rec_score": rec_score, "uninformative": uninformative}


# ---------------------------------------------------------------------------
# _compute_match_percentages() — pure function, no I/O
# ---------------------------------------------------------------------------

def test_best_and_worst_visible_pick_hit_100_and_0(m):
    entries = [_entry(10.0), _entry(55.0), _entry(100.0)]
    m._compute_match_percentages(entries)
    assert entries[0]["match_pct"] == 0
    assert entries[2]["match_pct"] == 100
    assert entries[1]["match_pct"] == 50


def test_single_informative_entry_is_100_percent(m):
    # hi == lo (only one score in the visible set) — the sole pick is, by
    # definition, the best of what's currently shown.
    entries = [_entry(42.0)]
    m._compute_match_percentages(entries)
    assert entries[0]["match_pct"] == 100


def test_all_tied_scores_are_100_percent(m):
    entries = [_entry(30.0), _entry(30.0), _entry(30.0)]
    m._compute_match_percentages(entries)
    assert all(e["match_pct"] == 100 for e in entries)


def test_uninformative_entries_get_none_and_are_excluded_from_range(m):
    entries = [_entry(0.0, uninformative=True), _entry(20.0), _entry(80.0)]
    m._compute_match_percentages(entries)
    assert entries[0]["match_pct"] is None
    # Range is computed only from the informative scores (20, 80), so the
    # uninformative 0.0 doesn't drag the floor down / skew the others.
    assert entries[1]["match_pct"] == 0
    assert entries[2]["match_pct"] == 100


def test_all_uninformative_entries_all_get_none(m):
    entries = [_entry(0.0, uninformative=True), _entry(0.0, uninformative=True)]
    m._compute_match_percentages(entries)
    assert all(e["match_pct"] is None for e in entries)


def test_empty_entries_list_is_a_noop(m):
    entries = []
    m._compute_match_percentages(entries)  # must not raise
    assert entries == []


def test_ranking_order_is_untouched(m):
    # Issue #227 is explicit: purely a display transform, no ranking change.
    # This just documents that the function only adds a key, never reorders
    # or drops entries.
    entries = [_entry(80.0), _entry(20.0), _entry(50.0)]
    ids_before = [id(e) for e in entries]
    m._compute_match_percentages(entries)
    assert [id(e) for e in entries] == ids_before
    assert len(entries) == 3


# ---------------------------------------------------------------------------
# GET /recommendations — real Jinja2 render via TestClient, no Postgres needed
# ---------------------------------------------------------------------------

LOW_ID = 911
MID_ID = 912
HIGH_ID = 913
COLD_START_ID = 914


def _row(anime_id, rec_score, uninformative=False):
    reason = (
        {"matched_genres": [], "matched_tags": [], "matched_studio": None,
         "cross_user_count": None, "cross_user_min_score": None}
        if uninformative else
        {"matched_genres": ["Action"], "matched_tags": [], "matched_studio": None,
         "cross_user_count": None, "cross_user_min_score": None}
    )
    return {
        "id": anime_id,
        "title_english": f"Show {anime_id}",
        "title_romaji": f"Show {anime_id}",
        "cover_image_url": None,
        "format": "TV",
        "episodes": 12,
        "average_score": None,
        "genres": ["Action"],
        "season": None,
        "season_year": None,
        "rec_score": rec_score,
        "reason": reason,
        "source": "similarity",
    }


@pytest.fixture()
def app_client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as main_module
    from fastapi.testclient import TestClient

    fake_user = {"id": 1, "email": "test@example.com", "is_admin": False}
    monkeypatch.setattr(main_module, "get_current_user", lambda request: fake_user)
    monkeypatch.setattr(main_module.config, "get", lambda user_id, key: main_module.config.DEFAULTS.get(key, ""))
    monkeypatch.setattr(main_module.db, "fetchall", lambda *a, **k: [])

    return TestClient(main_module.app), main_module


def _card_html(body: str, anime_id: int) -> str:
    marker = f'data-anime-id="{anime_id}"'
    start = body.index(marker)
    card_start = body.rindex('<div class="rec-card"', 0, start)
    next_card = body.find('<div class="rec-card"', start)
    return body[card_start:next_card] if next_card != -1 else body[card_start:]


def test_match_percent_chip_rendered_for_informative_entries(app_client, monkeypatch):
    client, m = app_client
    monkeypatch.setattr(
        m, "_fetch_visible_recommendations",
        lambda user_id: [_row(LOW_ID, 10.0), _row(MID_ID, 55.0), _row(HIGH_ID, 100.0)],
    )

    resp = client.get("/recommendations")
    assert resp.status_code == 200
    body = resp.text

    low_card = _card_html(body, LOW_ID)
    mid_card = _card_html(body, MID_ID)
    high_card = _card_html(body, HIGH_ID)

    assert 'match-percent-chip' in low_card
    assert '0% Match' in low_card
    assert '50% Match' in mid_card
    assert '100% Match' in high_card

    # The existing internal-score badge is untouched and still present alongside.
    assert 'rec-score-badge">10%</div>' in low_card
    assert 'rec-score-badge">100%</div>' in high_card


def test_match_percent_chip_absent_for_uninformative_entry(app_client, monkeypatch):
    client, m = app_client
    monkeypatch.setattr(
        m, "_fetch_visible_recommendations",
        lambda user_id: [_row(COLD_START_ID, 0.0, uninformative=True), _row(HIGH_ID, 90.0)],
    )

    resp = client.get("/recommendations")
    assert resp.status_code == 200
    body = resp.text

    cold_card = _card_html(body, COLD_START_ID)
    high_card = _card_html(body, HIGH_ID)

    assert 'match-percent-chip' not in cold_card
    assert 'match-percent-chip' in high_card
    assert '100% Match' in high_card
