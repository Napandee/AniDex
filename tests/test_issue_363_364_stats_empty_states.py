"""
Coverage for issues #363 and #364 — both found in the 2026-08-26 UI/UX audit.

#363: a brand-new account has no library_entries, so /api/stats returns empty
arrays for `status`/`genres`/`by_year` (not null — these three chart cards
render unconditionally, unlike the .stats-grid sections gated by #364 below).
Chart.js silently drew an axis-only canvas for an empty dataset with no
explanation. stats.html now hides the canvas and shows an empty-state <p> in
its place for these three, same pattern already used by the rewatch/
drop-patterns panels elsewhere on the page.

#364: `#studio-loyalty-section` (and every other `.stats-grid`-classed section
gated by JS — taste-drift, watch-activity, drop-patterns, rec-outcomes,
seasonal-follow-through) rendered as an empty box even while its `hidden`
attribute was set, because `.stats-grid { display: grid }` had no `[hidden]`
override and beat the browser's default. Fixed with a CSS rule, not testable
via a server-rendered-HTML assertion (that's a live-DOM/computed-style
question) — this file only verifies the `hidden` attribute itself is present
on page load, which is what the CSS fix now actually respects.

Real Postgres + real FastAPI TestClient, same pattern as
tests/test_stats_drilldown.py (this file's direct sibling — same fixtures).
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM invites")
        cur.execute("DELETE FROM users")


def _register_and_login(client, email="owner@example.com", password="correct horse battery staple"):
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


# ── #363 — empty-state markup for the three always-rendered charts ─────────────

def test_stats_page_renders_empty_state_markup_for_always_on_charts(client):
    _register_and_login(client)
    resp = client.get("/stats")
    assert resp.status_code == 200
    assert 'id="chart-status-empty"' in resp.text
    assert 'id="chart-genres-empty"' in resp.text
    assert 'id="chart-year-empty"' in resp.text
    # Server-rendered hidden by default — JS decides at runtime whether to
    # reveal them, same as every other empty-state <p> on this page.
    body = resp.text
    for empty_id in ("chart-status-empty", "chart-genres-empty", "chart-year-empty"):
        marker = f'id="{empty_id}"'
        idx = body.index(marker)
        tag_start = body.rindex("<p", 0, idx)
        tag_end = body.index(">", idx)
        assert "hidden" in body[tag_start:tag_end]


def test_stats_page_empty_state_text_is_real_not_placeholder(client):
    _register_and_login(client)
    resp = client.get("/stats")
    assert "Add anime to your library to see your status breakdown here." in resp.text
    assert "Complete a title to see your top genres here." in resp.text
    assert "Complete a title to see your completions by year here." in resp.text


# ── #364 — the sections gated by the [hidden]/.stats-grid CSS bug still carry
#           the hidden attribute server-side (the fix is CSS-only; this just
#           confirms the markup the CSS now correctly acts on didn't regress) ──

def test_studio_loyalty_section_hidden_by_default_for_new_account(client):
    _register_and_login(client)
    resp = client.get("/stats")
    assert resp.status_code == 200
    marker = 'id="studio-loyalty-section"'
    idx = resp.text.index(marker)
    tag_start = resp.text.rindex("<div", 0, idx)
    tag_end = resp.text.index(">", idx)
    tag = resp.text[tag_start:tag_end]
    assert "hidden" in tag
    assert "stats-grid" in tag


def test_all_gated_stats_grid_sections_carry_both_classes_and_hidden(client):
    """Every section #364's CSS fix needs to actually cover — confirms none of
    them lost the `stats-grid` class or the `hidden` attribute in this pass."""
    _register_and_login(client)
    resp = client.get("/stats")
    body = resp.text
    gated_section_ids = (
        "studio-loyalty-section",
        "taste-drift-section",
        "watch-activity-section",
        "drop-patterns-section",
        "rec-outcomes-section",
        "seasonal-follow-through-section",
    )
    for section_id in gated_section_ids:
        marker = f'id="{section_id}"'
        assert marker in body, f"{section_id} missing from rendered /stats"
        idx = body.index(marker)
        tag_start = body.rindex("<div", 0, idx)
        tag_end = body.index(">", idx)
        tag = body[tag_start:tag_end]
        assert "stats-grid" in tag, f"{section_id} lost its stats-grid class"
        assert "hidden" in tag, f"{section_id} lost its hidden attribute"
