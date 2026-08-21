"""
Coverage for issue #254's second part: a `recommendation_scores` row sourced from
the user's own Planning list is currently rendered identically to a genuine
"new discovery" pick on /recommendations — both get `source='similarity'` (see
run_recommender.py's `similarity_ids = rec_candidate_ids | planning_ids`), so
there's no way to tell them apart from `source` alone.

The fix is presentation-only, per the issue's explicit scope: no change to
run_recommender.py's candidate/scoring logic. `_fetch_visible_recommendations()`
(app/main.py) instead LEFT JOINs the *current* library_entries status live, so a
Planning-sourced pick stays correctly labeled even if the recommendation_scores
row itself hasn't been rewritten since the user added/removed it from Planning.

Two layers, mirroring tests/test_recommendation_match_percent.py's pattern:
  1. A real Postgres integration check that the LEFT JOIN actually produces
     `from_planning` correctly (skipped if no Postgres is reachable).
  2. GET /recommendations rendered through the app's real Jinja2Templates
     instance via TestClient (no Postgres) — a template change is only
     trustworthy once actually rendered and inspected, not just monkeypatched.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")


# ---------------------------------------------------------------------------
# _fetch_visible_recommendations — real Postgres, real LEFT JOIN
# ---------------------------------------------------------------------------

def _try_connect():
    try:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=2)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception:
        return None


@pytest.fixture(scope="module")
def pg_conn():
    conn = _try_connect()
    if conn is None:
        pytest.skip("no reachable Postgres for DATABASE_URL — skipping live DB check")
    yield conn
    conn.close()


PLANNING_ANIME_ID = 9100
DISCOVERY_ANIME_ID = 9101


@pytest.fixture()
def seeded(pg_conn):
    os.environ.setdefault("SESSION_SECRET_KEY", "test-key")
    from app.main import _fetch_visible_recommendations

    with pg_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM recommendation_scores WHERE anime_id = ANY(%s)",
            ([PLANNING_ANIME_ID, DISCOVERY_ANIME_ID],),
        )
        cur.execute(
            "DELETE FROM library_entries WHERE anime_id = ANY(%s)",
            ([PLANNING_ANIME_ID, DISCOVERY_ANIME_ID],),
        )
        cur.execute(
            "DELETE FROM anime WHERE id = ANY(%s)",
            ([PLANNING_ANIME_ID, DISCOVERY_ANIME_ID],),
        )
        cur.execute(
            "SELECT 1 FROM users WHERE id = 1"
        )
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO users (id, auth_provider, auth_provider_id, email, password_hash, is_admin) "
                "VALUES (1, 'local', 'local:1', 'test@example.com', 'x', true)"
            )
        for aid, title in [(PLANNING_ANIME_ID, "Planning Show"), (DISCOVERY_ANIME_ID, "Discovery Show")]:
            cur.execute(
                "INSERT INTO anime (id, title_romaji, format, status, episodes, genres, tags, studios, "
                "external_links, streaming_episodes, relations, last_synced_at) "
                "VALUES (%s, %s, 'TV', 'FINISHED', 12, '[]', '[]', '[]', '[]', '[]', '[]', now())",
                (aid, title),
            )
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress) VALUES (1, %s, 'PLANNING', 0)",
            (PLANNING_ANIME_ID,),
        )
        # Both written with source='similarity' — exactly today's real
        # run_recommender.py behavior (planning_ids merged into similarity_ids).
        for aid in (PLANNING_ANIME_ID, DISCOVERY_ANIME_ID):
            cur.execute(
                "INSERT INTO recommendation_scores "
                "(user_id, anime_id, score, reason, source, dismissed, computed_at, first_shown_at) "
                "VALUES (1, %s, 80, '{}', 'similarity', false, now(), now())",
                (aid,),
            )

    return _fetch_visible_recommendations


def test_planning_sourced_row_flagged_from_planning_despite_identical_source(seeded):
    rows = seeded(1)
    by_id = {r["id"]: r for r in rows}
    assert by_id[PLANNING_ANIME_ID]["source"] == "similarity"
    assert by_id[DISCOVERY_ANIME_ID]["source"] == "similarity"
    assert by_id[PLANNING_ANIME_ID]["from_planning"] is True
    assert by_id[DISCOVERY_ANIME_ID]["from_planning"] is False


# ---------------------------------------------------------------------------
# GET /recommendations — real Jinja2 render via TestClient, no Postgres needed
# ---------------------------------------------------------------------------

PLANNING_ID = 921
DISCOVERY_ID = 922


def _row(anime_id, from_planning):
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
        "rec_score": 50.0,
        "reason": {"matched_genres": ["Action"], "matched_tags": [], "matched_studio": None,
                    "cross_user_count": None, "cross_user_min_score": None},
        "source": "similarity",
        "from_planning": from_planning,
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


def test_planning_sourced_card_shows_queue_chip_and_hides_add_planning_button(app_client, monkeypatch):
    client, m = app_client
    monkeypatch.setattr(
        m, "_fetch_visible_recommendations",
        lambda user_id: [_row(PLANNING_ID, from_planning=True), _row(DISCOVERY_ID, from_planning=False)],
    )

    resp = client.get("/recommendations")
    assert resp.status_code == 200
    body = resp.text

    planning_card = _card_html(body, PLANNING_ID)
    discovery_card = _card_html(body, DISCOVERY_ID)

    assert "rec-planning-chip" in planning_card
    assert "btn-add-planning" not in planning_card

    assert "rec-planning-chip" not in discovery_card
    assert "btn-add-planning" in discovery_card
