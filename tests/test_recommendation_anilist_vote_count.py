"""
Coverage for issue #226 — surface AniList's own per-edge recommendation vote count
(`Recommendation.rating`) in the existing recommendation reason chips on
/recommendations, for candidates actually discovered via AniList's `recommendations`
edge. No fabricated count for candidates sourced elsewhere (the user's own PLANNING
list, cross-user signal #31, or the seasonal digest #13) — score_and_store() only
ever sets `reason.anilist_vote_count` for anime_ids present in the vote_counts map
built from that specific GraphQL response (see test_run_recommender.py for the
scripts/run_recommender.py-side coverage of that map's construction).

Same rendering-layer pattern as test_cold_start_recommendation_badge.py: the real
Jinja2Templates instance via TestClient, not a stubbed render, per this repo's
standing "test the right route" lesson (CLAUDE.local.md) — a template change isn't
trustworthy on the strength of "it imports" or "returned 200".
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")

WITH_VOTES_ID = 950
NO_VOTES_ID = 951


def _reason(anilist_vote_count=None, **overrides):
    base = {
        "matched_genres": ["Action"],
        "matched_tags": [],
        "matched_studio": None,
        "cross_user_count": None,
        "cross_user_min_score": None,
        "anilist_vote_count": anilist_vote_count,
    }
    base.update(overrides)
    return base


def _row(anime_id, reason, source="similarity"):
    return {
        "id": anime_id,
        "title_english": None,
        "title_romaji": f"Show {anime_id}",
        "cover_image_url": None,
        "format": "TV",
        "episodes": 12,
        "average_score": None,
        "genres": ["Action"],
        "season": None,
        "season_year": None,
        "rec_score": 55.0,
        "reason": reason,
        "source": source,
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


def test_card_with_anilist_vote_count_renders_the_chip(app_client, monkeypatch):
    client, m = app_client
    monkeypatch.setattr(
        m,
        "_fetch_visible_recommendations",
        lambda user_id: [_row(WITH_VOTES_ID, _reason(anilist_vote_count=1204))],
    )

    resp = client.get("/recommendations")
    assert resp.status_code == 200
    card = _card_html(resp.text, WITH_VOTES_ID)

    assert "1204" in card
    assert "AniList" in card


def test_card_without_anilist_vote_count_has_no_fabricated_chip(app_client, monkeypatch):
    client, m = app_client
    monkeypatch.setattr(
        m,
        "_fetch_visible_recommendations",
        lambda user_id: [_row(NO_VOTES_ID, _reason(anilist_vote_count=None))],
    )

    resp = client.get("/recommendations")
    assert resp.status_code == 200
    card = _card_html(resp.text, NO_VOTES_ID)

    # No vote-count row at all for a candidate that never had real AniList vote
    # data (e.g. PLANNING-list or cross-user-sourced) — not a "0" or a "—" placeholder.
    assert "rec_match_anilist_votes" not in card  # key itself never leaks unrendered
    assert "AniList votes" not in card


def test_singular_vote_count_uses_singular_string(app_client, monkeypatch):
    client, m = app_client
    monkeypatch.setattr(
        m,
        "_fetch_visible_recommendations",
        lambda user_id: [_row(WITH_VOTES_ID, _reason(anilist_vote_count=1))],
    )

    resp = client.get("/recommendations")
    assert resp.status_code == 200
    card = _card_html(resp.text, WITH_VOTES_ID)

    assert "1 AniList user agrees" in card
    assert "1 AniList users agree" not in card


def test_mixed_batch_only_the_recs_edge_sourced_card_shows_the_chip(app_client, monkeypatch):
    client, m = app_client
    monkeypatch.setattr(
        m,
        "_fetch_visible_recommendations",
        lambda user_id: [
            _row(WITH_VOTES_ID, _reason(anilist_vote_count=1204)),
            _row(NO_VOTES_ID, _reason(anilist_vote_count=None), source="seasonal"),
        ],
    )

    resp = client.get("/recommendations")
    assert resp.status_code == 200
    body = resp.text

    with_card = _card_html(body, WITH_VOTES_ID)
    without_card = _card_html(body, NO_VOTES_ID)

    assert "1204 AniList users agree" in with_card
    assert "AniList votes" not in without_card
