"""
Coverage for issue #268 — collapsed credential cards (AniList/Crunchyroll/Netflix on
/settings) still showed a preview of their first form field even while collapsed.

Root cause: `.cred-card-body`/`.cred-guide` (`app/static/style.css`) are the
`.help-disclosure-panel` grid containers whose row track the 0fr/1fr trick collapses
to zero height when not `.open`. The padding/flex layout for their content
(`.cred-card-body > div`, `.cred-guide > div`) was declared directly on the same
`<div>` that also serves as the trick's `overflow: hidden` clip host
(`.help-disclosure-panel > div`) — but a collapsing grid/flex item's own padding
always contributes to its rendered size regardless of `overflow: hidden`; only its
*content* gets zeroed by the automatic-minimum-size mechanism. So the collapsed body
still rendered a slice exactly as tall as its own padding, with the first line of
content (a `<label>`) bleeding into that slice before the rest got clipped.

Fix: added one more nested `<div>` in `app/templates/settings.html` so the padding
lives one level deeper than the clip host, matching the pattern
`.help-disclosure-panel-body` (the plain "i"-button disclosures) already used
correctly. This is a template/CSS structural regression guard — same
real-Postgres + real-TestClient pattern as tests/test_settings_defaults.py, skipped
entirely if no Postgres is reachable.
"""

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


def _try_connect():
    try:
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
        pytest.skip(
            f"No reachable Postgres at {DATABASE_URL} — this suite needs a real "
            "throwaway instance (same one .github/workflows/pr-validate.yml provisions)."
        )
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(SCHEMA_SQL)
    yield conn
    conn.close()


@pytest.fixture()
def app_module(pg_conn, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    return m


@pytest.fixture(autouse=True)
def _clean_tables(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM users")


@pytest.fixture()
def client(app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


def _register_and_login(client, email, password="correct horse battery staple"):
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


# Each credential card body's clip host (the `.help-disclosure-panel > div` that
# `overflow: hidden` is declared on) must have no styled content as a direct child —
# padding/flex-layout content must sit one level deeper, or it bleeds through while
# collapsed. This checks the actual nesting depth in the rendered HTML rather than
# just the outer div's classes, since that's exactly what regressed in #268.
CRED_BODY_IDS = ["cred-body-anilist", "cred-body-crunchyroll", "cred-body-netflix"]
CRED_GUIDE_IDS = ["cred-guide-anilist", "cred-guide-crunchyroll", "cred-guide-netflix"]


def test_cred_card_bodies_nest_content_below_the_clip_host(app_module, client):
    _register_and_login(client, "owner@example.com")

    resp = client.get("/settings")
    assert resp.status_code == 200
    html = resp.text

    for body_id in CRED_BODY_IDS:
        marker_present = f'id="{body_id}"><div>' in html
        assert marker_present, f'id="{body_id}" not found in rendered HTML at all'
        assert f'id="{body_id}"><div><div>' in html, (
            f'expected id="{body_id}" to open with an extra nested <div><div> layer '
            "(the padded/flex content wrapper) below the overflow:hidden clip host — "
            "if this doesn't match, the collapsed card body may bleed through again (#268)"
        )


def test_cred_guide_panels_nest_content_below_the_clip_host(app_module, client):
    _register_and_login(client, "owner@example.com")

    resp = client.get("/settings")
    assert resp.status_code == 200
    html = resp.text

    for guide_id in CRED_GUIDE_IDS:
        marker_open = f'id="{guide_id}"><div><div>'
        marker_close_present = f'id="{guide_id}"><div>' in html
        assert marker_close_present, f'id="{guide_id}" not found in rendered HTML at all'
        assert marker_open in html, (
            f'expected id="{guide_id}" to open with an extra nested <div><div> layer '
            "so the guide's background/border/padding sits below the overflow:hidden "
            "clip host, not on it — otherwise the collapsed guide sub-panel bleeds "
            "through its first line of text (#268)"
        )
