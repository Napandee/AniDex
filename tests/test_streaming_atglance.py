"""
Coverage for issue #270 — /streaming restructure around a synthesized "at a glance"
summary card, with the page's other sections grouped and collapsed to progressive
disclosure.

Two layers:
  1. `_compute_streaming_atglance` is pure Python (no DB) — it only composes fields
     already present in the `setcover`/`cancel` dicts `_compute_streaming_setcover`
     and `_compute_streaming_cancel_candidates` return, so it's tested directly with
     hand-built dicts, same pattern as `_greedy_set_cover` in
     tests/test_streaming_setcover.py. This is where #270's explicit
     acceptance-criteria edge cases live: no owned services at all, coverage already
     at 100%, nothing safe to cancel, and no swap candidate.
  2. One DB-backed end-to-end render check (real Postgres, same throwaway-instance
     pattern as the rest of this streaming test cluster) confirming the at-a-glance
     card and the new collapsed-group markup actually appear on `/streaming`.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_fn():
    """Import lazily so this module can still be collected (and the pure-Python
    section below still run) on a machine with no Postgres at all — only the
    DB-backed test at the bottom skips."""
    import app.main as m

    return m._compute_streaming_atglance


def _setcover(owned=None, total_universe_episodes=0, owned_total_covered_episodes=0, swap_candidates=None):
    return {
        "owned": owned or [],
        "total_universe_episodes": total_universe_episodes,
        "owned_total_covered_episodes": owned_total_covered_episodes,
        "swap_candidates": swap_candidates or [],
    }


def _cancel(candidates=None):
    return {"candidates": candidates or []}


# ── 1. No owned services at all ──────────────────────────────────────────────────

def test_no_owned_services_degrades_every_clause():
    fn = _load_fn()
    result = fn(
        _setcover(owned=[], total_universe_episodes=100, owned_total_covered_episodes=0, swap_candidates=[]),
        _cancel(candidates=[]),
    )

    assert result["has_owned"] is False
    assert result["owned_count"] == 0
    assert result["fully_covered"] is False
    # Nothing to cancel or swap when there's nothing owned in the first place —
    # both clauses must come back None, not a broken/empty-string placeholder.
    assert result["cancel_service"] is None
    assert result["swap_service"] is None
    assert result["swap_episodes"] is None


# ── 2. Coverage already 100% ─────────────────────────────────────────────────────

def test_fully_covered_suppresses_swap_clause_even_if_a_candidate_exists():
    """A swap candidate can still exist numerically once coverage is already
    complete (e.g. an un-owned service ties the owned total by covering the exact
    same titles) — #270's acceptance criteria requires the sentence to still read
    sensibly, so the swap clause must be suppressed once there's nothing left to
    add for, regardless of what swap_candidates contains."""
    fn = _load_fn()
    result = fn(
        _setcover(
            owned=["Crunchyroll"],
            total_universe_episodes=50,
            owned_total_covered_episodes=50,
            swap_candidates=[{"service": "Netflix", "episodes": 50, "count": 3}],
        ),
        _cancel(candidates=[{"service": "Crunchyroll", "fully_redundant": False}]),
    )

    assert result["fully_covered"] is True
    assert result["coverage_pct"] == 100.0
    assert result["swap_service"] is None
    assert result["swap_episodes"] is None
    # Cancel is independent of coverage completeness — Crunchyroll is the only
    # owned service and isn't redundant, so it correctly stays unset here too.
    assert result["cancel_service"] is None


# ── 3. Nothing safe to cancel ─────────────────────────────────────────────────────

def test_top_cancel_candidate_not_fully_redundant_yields_no_cancel_clause():
    fn = _load_fn()
    result = fn(
        _setcover(owned=["A", "B"], total_universe_episodes=100, owned_total_covered_episodes=80, swap_candidates=[]),
        _cancel(candidates=[
            {"service": "A", "fully_redundant": False},
            {"service": "B", "fully_redundant": False},
        ]),
    )

    assert result["cancel_service"] is None
    # Coverage clause is unaffected — still reads normally.
    assert result["fully_covered"] is False
    assert result["coverage_pct"] == 80.0


def test_no_cancel_candidates_at_all_yields_no_cancel_clause():
    fn = _load_fn()
    result = fn(
        _setcover(owned=["A"], total_universe_episodes=10, owned_total_covered_episodes=10, swap_candidates=[]),
        _cancel(candidates=[]),
    )
    assert result["cancel_service"] is None


# ── 4. No swap candidate beats current coverage ───────────────────────────────────

def test_no_swap_candidates_yields_no_swap_clause_but_cancel_still_shows():
    fn = _load_fn()
    result = fn(
        _setcover(owned=["A"], total_universe_episodes=100, owned_total_covered_episodes=40, swap_candidates=[]),
        _cancel(candidates=[{"service": "A", "fully_redundant": True}]),
    )

    assert result["swap_service"] is None
    assert result["swap_episodes"] is None
    # Independent clause — still populated even though swap has nothing to say.
    assert result["cancel_service"] == "A"


# ── 5. Empty universe (no backlog at all) — division-by-zero guard ───────────────

def test_empty_universe_does_not_divide_by_zero():
    fn = _load_fn()
    result = fn(
        _setcover(owned=["A"], total_universe_episodes=0, owned_total_covered_episodes=0, swap_candidates=[]),
        _cancel(candidates=[]),
    )
    assert result["coverage_pct"] == 0.0
    assert result["fully_covered"] is False
    assert result["total_episodes"] == 0


# ── 6. Full happy path — every clause populated ───────────────────────────────────

def test_full_happy_path_populates_every_clause():
    fn = _load_fn()
    result = fn(
        _setcover(
            owned=["Crunchyroll", "Netflix"],
            total_universe_episodes=100,
            owned_total_covered_episodes=33,
            swap_candidates=[{"service": "Hulu", "episodes": 40, "count": 5}],
        ),
        _cancel(candidates=[
            {"service": "Netflix", "fully_redundant": True},
            {"service": "Crunchyroll", "fully_redundant": False},
        ]),
    )

    assert result["has_owned"] is True
    assert result["owned_count"] == 2
    assert result["coverage_pct"] == 33.0
    assert result["fully_covered"] is False
    assert result["cancel_service"] == "Netflix"
    assert result["swap_service"] == "Hulu"
    assert result["swap_episodes"] == 40


# ── 7. /streaming end-to-end render (real Postgres) ───────────────────────────────

USER_ID = 1


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM user_streaming_services")
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM users")


def _register_and_login(client, email="glance@example.com", password="correct horse battery staple"):
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def test_streaming_page_renders_glance_card_and_collapsed_groups(pg_conn, app_module, client):
    _register_and_login(client)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = 'glance@example.com'")
        (uid,) = cur.fetchone()

    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, external_links, episodes) VALUES "
            "(1, 'Owned Title', '[{\"site\": \"Crunchyroll\"}]'::jsonb, 12)"
        )
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress) VALUES (%s, 1, 'WATCHING', 3)",
            (uid,),
        )
        cur.execute(
            "INSERT INTO user_streaming_services (user_id, service) VALUES (%s, 'Crunchyroll')",
            (uid,),
        )

    resp = client.get("/streaming")
    assert resp.status_code == 200

    # At-a-glance card present, above everything else. (Search the rendered body
    # markup, not window.I18N's embedded JSON blob in <head> — several i18n
    # strings themselves mention "Your Setup" in prose, e.g. streaming_glance_no_setup.)
    assert 'class="streaming-glance-card"' in resp.text
    glance_pos = resp.text.index('streaming-glance-card')
    setup_pos = resp.text.index('<h2 class="streaming-group-eyebrow">Your Setup</h2>')
    assert glance_pos < setup_pos

    # "Your Setup" renders expanded — not inside a <details> collapse.
    assert '<h2 class="streaming-group-eyebrow">Your Setup</h2>' in resp.text

    # The other four groups render as collapsed <details> with a group-eyebrow
    # label and a one-line summary, expandable to their existing content.
    for label in ["Recommendations", "Explore Coverage", "Timing", "History"]:
        assert f'<span class="streaming-group-eyebrow">{label}</span>' in resp.text
    assert 'streaming-group-details' in resp.text

    # The old #182 bar-chart-only "If you added…" section no longer exists.
    assert "If you added" not in resp.text
    assert 'streaming_ranked_title' not in resp.text