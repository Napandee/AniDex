"""demo/seed.sql — the made-up data the static demo is recorded from.

Loads schema.sql (cumulative: it already contains every migrations/ file, which
is how compose/dev.yml and pr-validate.yml build a fresh database) plus the seed
into the test Postgres, bound to a registered demo user exactly as
demo/record.mjs binds it.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SEED_SQL = (ROOT / "demo" / "seed.sql").read_text()
DEV_SEED_SQL = (ROOT / "scripts" / "dev_seed.sql").read_text()
CONFIG = json.loads((ROOT / "demo" / "demo.config.json").read_text())
EMAIL = CONFIG["user"]["email"]
PASSWORD = CONFIG["user"]["password"]

LIBRARY_STATUSES = {"WATCHING", "COMPLETED", "DROPPED", "PLANNING", "PAUSED", "REPEATING"}


def bind_seed(sql: str, email: str) -> str:
    """Same binding demo/record.mjs applies: psql's :'email' variable, quoted."""
    return sql.replace(":'email'", "'" + email.replace("'", "''") + "'")


@pytest.fixture(scope="module")
def seeded(pg_conn, app_module_mod):
    from starlette.testclient import TestClient

    with TestClient(app_module_mod.app) as c:
        resp = c.post(
            "/auth/register",
            data={"email": EMAIL, "password": PASSWORD},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        with pg_conn.cursor() as cur:
            cur.execute(bind_seed(SEED_SQL, EMAIL))
            cur.execute("SELECT id FROM users WHERE email = %s", (EMAIL,))
            uid = cur.fetchone()[0]
        yield c, uid


@pytest.fixture(scope="module")
def app_module_mod(pg_conn):
    import os

    os.environ.setdefault("SESSION_SECRET_KEY", "test-key")
    import app.main as m

    return m


def _q(pg_conn, sql, params=()):
    with pg_conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def test_demo_seed_loads(seeded, pg_conn):
    _, uid = seeded
    (n,) = _q(pg_conn, "SELECT COUNT(*) FROM library_entries WHERE user_id = %s", (uid,))[0]
    assert n >= 25
    (titles,) = _q(pg_conn, "SELECT COUNT(*) FROM anime")[0]
    assert titles >= 30


def test_demo_seed_is_made_up(seeded, pg_conn):
    dev_titles = {
        t.lower()
        for pair in re.findall(r"\(\s*\d+\s*,\s*'([^']*)'\s*,\s*'([^']*)'", DEV_SEED_SQL)
        for t in pair
    }
    assert dev_titles, "could not parse scripts/dev_seed.sql titles"
    rows = _q(pg_conn, "SELECT id, title_romaji, title_english, title_native, cover_image_url, banner_image_url FROM anime")
    for anime_id, romaji, english, native, cover, banner in rows:
        for t in (romaji, english, native):
            if t:
                assert t.lower() not in dev_titles, f"{t!r} is a real title from dev_seed.sql"
        assert cover == f"/static/demo-covers/{anime_id}.svg", (anime_id, cover)
        assert banner is None
    # No third-party hosts anywhere in the seed (links, images, fonts).
    hosts = set(re.findall(r"https?://([^/'\"\s]+)", SEED_SQL))
    assert all(h.endswith(".invalid") for h in hosts), hosts


def test_demo_seed_covers_every_status(seeded, pg_conn):
    client, uid = seeded
    statuses = {r[0] for r in _q(pg_conn, "SELECT DISTINCT status FROM library_entries WHERE user_id = %s", (uid,))}
    assert LIBRARY_STATUSES <= statuses, LIBRARY_STATUSES - statuses

    (notes,) = _q(pg_conn, "SELECT COUNT(*) FROM personal_notes WHERE user_id = %s AND notes IS NOT NULL", (uid,))[0]
    assert notes >= 3
    (tagged,) = _q(pg_conn, "SELECT COUNT(*) FROM personal_notes WHERE user_id = %s AND jsonb_array_length(personal_tags) > 0", (uid,))[0]
    assert tagged >= 3
    (collections,) = _q(pg_conn, "SELECT COUNT(*) FROM collections WHERE user_id = %s", (uid,))[0]
    assert collections >= 1
    (recs,) = _q(pg_conn, "SELECT COUNT(*) FROM recommendation_scores WHERE user_id = %s", (uid,))[0]
    assert recs >= 3
    (airing,) = _q(pg_conn, "SELECT COUNT(*) FROM airing_schedule_cache WHERE airing_at > now()")[0]
    assert airing >= 3

    # /stats charts come from /api/stats; /stats/wrapped is server-rendered.
    stats = client.get("/api/stats")
    assert stats.status_code == 200, stats.text
    assert stats.json(), "empty /api/stats"
    wrapped = client.get("/stats/wrapped")
    assert wrapped.status_code == 200
    from app import main as m

    page = m._compute_wrapped_page(uid)
    assert page["has_data"], "wrapped would render its empty state"
    assert page["top_genre"] and page["highest_rated"] and page["most_rewatched"]
    assert m._yearly_completion_rows(uid, page["prior_year"]), "no prior-year history"


def test_demo_covers_match_seed(seeded, pg_conn, tmp_path):
    import subprocess
    import sys

    subprocess.run(
        [sys.executable, str(ROOT / "demo" / "make_covers.py"), str(ROOT / "demo" / "seed.sql"), str(tmp_path)],
        check=True,
    )
    ids = {r[0] for r in _q(pg_conn, "SELECT id FROM anime")}
    files = {int(p.stem) for p in tmp_path.glob("*.svg")}
    assert files == ids
    first = (tmp_path / "900001.svg").read_text()
    assert first.startswith("<svg") and ">SD<" in first and "http" not in first.replace("http://www.w3.org/2000/svg", "")
    # Deterministic: a second run writes the same bytes.
    again = tmp_path / "again"
    subprocess.run(
        [sys.executable, str(ROOT / "demo" / "make_covers.py"), str(ROOT / "demo" / "seed.sql"), str(again)],
        check=True,
    )
    assert (again / "900001.svg").read_text() == first
