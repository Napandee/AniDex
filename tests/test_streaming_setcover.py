"""
Coverage for issue #255 — Streaming Coverage v3: minimal-service-set recommendation
(set-cover), title-level breakdown, and dedicated nav tab.

Two layers, matching the split in app/main.py:
  1. `_greedy_set_cover` is pure Python (no DB) — tested directly with plain dicts/sets
     for the exact greedy-selection and alphabetical-tie-break behavior.
  2. `_streaming_universe` / `_compute_streaming_setcover` need a real Postgres, same
     throwaway-instance pattern as tests/test_streaming_coverage.py (#182) and
     tests/test_streaming_calendar.py (#228) — skipped entirely if one isn't reachable.

Covers the acceptance-criteria scenarios from issue #255:
  1. Greedy set-cover picks the minimal owned combination on a realistic multi-service
     overlap (redundant service correctly dropped), with a documented alphabetical
     tie-break rule.
  2. Coverage universe = Watching + Planning + Upcoming (a non-Watching/Planning title
     with a still-unaired episode), Completed excluded even when it also has an
     upcoming episode; a tracked title with neither an active status nor any upcoming
     episode is invisible to the universe entirely.
  3. Titles on no recognized streaming service land in an explicit, non-empty
     `uncovered_titles` bucket rather than being silently dropped from any count.
  3b. Every title chip in `uncovered_titles` / `marginal[*].titles` / `ranked_all[*].titles`
      is a `{id, title, status}` dict carrying the REAL anime id (#271 — makes the
      title clickable through to `/anime/{id}/notes`), not a bare string, and the id
      is verified to match the right anime, not just "some id present".
  4. Per-owned-service marginal/unique contribution: a service redundant with another
     owned service reports zero unique titles.
  5. The full ranked list includes every STREAMING_SITES entry, owned and unowned,
     with no coverage-threshold cutoff (including zero-coverage services).
  6. A swap/consolidation suggestion appears when an unowned service's total coverage
     alone matches or beats the current owned combination's total coverage, and does
     not appear otherwise.
  7. `/streaming` renders the new set-cover sections end-to-end for a logged-in user.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://test:test@localhost/test")
SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()

USER_ID = 1


# ── 1. Pure-Python greedy set-cover — no DB needed ──────────────────────────────

def _load_greedy():
    """Import lazily so this module can still be collected (and this section run)
    on a machine with no Postgres at all — only the DB-backed tests below skip."""
    import app.main as m

    return m._greedy_set_cover


def test_greedy_set_cover_drops_a_redundant_service():
    """Realistic multi-service scenario: Crunchyroll covers 4 of 5 titles, Hulu
    covers the 5th, Netflix's entire coverage is a strict subset of Crunchyroll's
    (no unique titles) — the greedy algorithm must select {Crunchyroll, Hulu} and
    correctly drop Netflix as redundant, not include all three just because it's
    "owned"."""
    greedy = _load_greedy()
    id_to_sites = {
        1: {"Crunchyroll", "Netflix"},
        2: {"Crunchyroll"},
        3: {"Crunchyroll", "Netflix"},
        4: {"Hulu"},
        5: {"Crunchyroll", "Hulu"},
    }
    result = greedy(["Netflix", "Crunchyroll", "Hulu"], set(id_to_sites), id_to_sites)
    assert result == ["Crunchyroll", "Hulu"]


def test_greedy_set_cover_tie_break_is_alphabetical():
    """Two services with identical coverage: the earlier one alphabetically wins the
    tie deterministically (issue #255's own open question leaves the exact
    tie-break rule to the implementer; this is the rule chosen and it must be
    stable)."""
    greedy = _load_greedy()
    id_to_sites = {1: {"Beta", "Alpha"}, 2: {"Beta", "Alpha"}}
    result = greedy(["Beta", "Alpha"], set(id_to_sites), id_to_sites)
    assert result == ["Alpha"]  # covers everything alone — Beta never needed


def test_greedy_set_cover_second_round_tie_break():
    """After the first (unambiguous) pick, a genuine tie in the second round is
    still broken alphabetically, not by iteration/insertion order."""
    greedy = _load_greedy()
    id_to_sites = {
        1: {"Crunchyroll"},
        2: {"Crunchyroll"},
        3: {"Zebra"},
        4: {"Amber"},
    }
    # Crunchyroll covers {1,2} first (size 2, uniquely largest — Zebra/Amber only
    # cover 1 each at this point). Remaining {3,4}: Zebra covers {3}, Amber covers
    # {4} — tie of size 1, "Amber" < "Zebra" alphabetically.
    result = greedy(["Zebra", "Crunchyroll", "Amber"], set(id_to_sites), id_to_sites)
    assert result == ["Crunchyroll", "Amber", "Zebra"]


def test_greedy_set_cover_empty_universe_selects_nothing():
    greedy = _load_greedy()
    assert greedy(["Crunchyroll", "Netflix"], set(), {}) == []


# ── 2+. DB-backed: universe, uncovered bucket, marginal, ranked list, swap ──────

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
        cur.execute("DELETE FROM airing_schedule_cache")
        cur.execute("DELETE FROM user_streaming_services")
        cur.execute("DELETE FROM library_entries")
        cur.execute("DELETE FROM anime")
        cur.execute("DELETE FROM users")


@pytest.fixture()
def _seeded_user(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, auth_provider, auth_provider_id, email) "
            "VALUES (%s, 'local', 'test', 'test@example.com')",
            (USER_ID,),
        )


def _insert_anime(pg_conn, anime_id, sites, title=None):
    links = [{"site": s, "url": f"https://example.com/{anime_id}"} for s in sites]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO anime (id, title_romaji, external_links) VALUES (%s, %s, %s::jsonb)",
            (anime_id, title or f"Anime {anime_id}", json.dumps(links)),
        )


def _insert_entry(pg_conn, anime_id, status, user_id=USER_ID):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (user_id, anime_id, status, progress) VALUES (%s, %s, %s, 0)",
            (user_id, anime_id, status),
        )


def _insert_airing(pg_conn, anime_id, episode, days_from_now):
    airing_at = datetime.now(timezone.utc) + timedelta(days=days_from_now)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO airing_schedule_cache (anime_id, episode, airing_at) VALUES (%s, %s, %s)",
            (anime_id, episode, airing_at),
        )


def _set_owned(pg_conn, services, user_id=USER_ID):
    with pg_conn.cursor() as cur:
        for s in services:
            cur.execute(
                "INSERT INTO user_streaming_services (user_id, service) VALUES (%s, %s)",
                (user_id, s),
            )


def _seed_realistic_library(pg_conn):
    """The scenario used by most tests below:

      T1 WATCHING  Crunchyroll + Netflix + Bilibili TV
      T2 WATCHING  Crunchyroll + Bilibili TV
      T3 PLANNING  Crunchyroll + Netflix + Bilibili TV   (Netflix has zero unique titles)
      T4 PLANNING  Hulu + Bilibili TV
      T5 WATCHING  Crunchyroll + Hulu
      T6 PLANNING  Max + Bilibili TV                      (unowned-only, low coverage)
      T7 WATCHING  no recognized streaming site at all    (the honest "uncovered" bucket)
      T8 PAUSED    Disney Plus, WITH an upcoming episode   ("Upcoming" universe inclusion)
      T9 PAUSED    Netflix, NO upcoming episode            (must be invisible entirely)
      T10 COMPLETED Crunchyroll, WITH an upcoming episode  (Completed excluded regardless)

    Owned: Crunchyroll, Netflix, Hulu.
      owned-coverable = {T1,T2,T3,T4,T5} = 5 titles.
      Greedy: Crunchyroll covers {T1,T2,T3,T5}=4 (picked first) -> remaining {T4}
              -> Hulu covers {T4}=1 (picked) -> remaining empty.
      Minimal combination = [Crunchyroll, Hulu]. Netflix is redundant (0 unique).
      Bilibili TV (unowned) covers {T1,T2,T3,T4,T6} = 5, matching owned_total_covered
      (5) exactly -> qualifies as a swap candidate ("matches or beats").
    """
    _insert_anime(pg_conn, 1, ["Crunchyroll", "Netflix", "Bilibili TV"], title="T1")
    _insert_entry(pg_conn, 1, "WATCHING")
    _insert_anime(pg_conn, 2, ["Crunchyroll", "Bilibili TV"], title="T2")
    _insert_entry(pg_conn, 2, "WATCHING")
    _insert_anime(pg_conn, 3, ["Crunchyroll", "Netflix", "Bilibili TV"], title="T3")
    _insert_entry(pg_conn, 3, "PLANNING")
    _insert_anime(pg_conn, 4, ["Hulu", "Bilibili TV"], title="T4")
    _insert_entry(pg_conn, 4, "PLANNING")
    _insert_anime(pg_conn, 5, ["Crunchyroll", "Hulu"], title="T5")
    _insert_entry(pg_conn, 5, "WATCHING")
    _insert_anime(pg_conn, 6, ["Max", "Bilibili TV"], title="T6")
    _insert_entry(pg_conn, 6, "PLANNING")
    _insert_anime(pg_conn, 7, ["SomeRandomWiki"], title="T7")
    _insert_entry(pg_conn, 7, "WATCHING")
    _insert_anime(pg_conn, 8, ["Disney Plus"], title="T8")
    _insert_entry(pg_conn, 8, "PAUSED")
    _insert_airing(pg_conn, 8, episode=5, days_from_now=10)
    _insert_anime(pg_conn, 9, ["Netflix"], title="T9")
    _insert_entry(pg_conn, 9, "PAUSED")
    _insert_anime(pg_conn, 10, ["Crunchyroll"], title="T10")
    _insert_entry(pg_conn, 10, "COMPLETED")
    _insert_airing(pg_conn, 10, episode=1, days_from_now=5)

    _set_owned(pg_conn, ["Crunchyroll", "Netflix", "Hulu"])


def _titles(chips):
    """Extract the bare title strings from a list of {id, title, status} chip dicts,
    for tests that only care about which titles are present, not their ids."""
    return {c["title"] for c in chips}


def test_universe_includes_watching_planning_and_upcoming_excludes_completed(
    pg_conn, app_module, _seeded_user
):
    _seed_realistic_library(pg_conn)

    universe = app_module._streaming_universe(USER_ID)
    titles = {t["title"] for t in universe}

    # T1-T8 all present (T8 via the "Upcoming" PAUSED-but-airing-soon path).
    assert titles == {f"T{i}" for i in range(1, 9)}
    # T9 (PAUSED, no upcoming episode) and T10 (COMPLETED, even with an upcoming
    # episode) must both be entirely absent from the universe.
    assert "T9" not in titles
    assert "T10" not in titles


def test_setcover_minimal_combination_drops_redundant_owned_service(
    pg_conn, app_module, _seeded_user
):
    _seed_realistic_library(pg_conn)

    result = app_module._compute_streaming_setcover(USER_ID)

    assert result["owned_total_covered"] == 5
    assert result["minimal_combination"] == ["Crunchyroll", "Hulu"]


def test_setcover_uncovered_bucket_is_explicit_not_silently_dropped(
    pg_conn, app_module, _seeded_user
):
    _seed_realistic_library(pg_conn)

    result = app_module._compute_streaming_setcover(USER_ID)

    # {id, title, status} dicts (#271), not bare strings — and the id must be the
    # REAL anime id (T7 was inserted as anime id 7), so the template's link to
    # /anime/{id}/notes actually points at the right title.
    assert result["uncovered_titles"] == [{"id": 7, "title": "T7", "status": "WATCHING"}]
    # Universe size counts every tracked title, covered or not.
    assert result["universe_size"] == 8


def test_setcover_marginal_contribution_zero_for_redundant_service(
    pg_conn, app_module, _seeded_user
):
    _seed_realistic_library(pg_conn)

    result = app_module._compute_streaming_setcover(USER_ID)
    marginal = {m["service"]: m for m in result["marginal"]}

    assert set(marginal) == {"Crunchyroll", "Netflix", "Hulu"}
    # Netflix's entire coverage ({T1,T3}) is a strict subset of Crunchyroll's —
    # zero titles reachable ONLY through Netflix among the owned services.
    assert marginal["Netflix"]["count"] == 0
    assert marginal["Netflix"]["titles"] == []
    # Hulu uniquely covers T4 (T5 is shared with Crunchyroll). {id, title, status}
    # dicts now (#271) — assert the real anime id (4), not just the title string.
    assert marginal["Hulu"]["count"] == 1
    assert marginal["Hulu"]["titles"] == [{"id": 4, "title": "T4", "status": "PLANNING"}]
    # Crunchyroll uniquely covers T2 (T1/T3 shared w/ Netflix, T5 shared w/ Hulu).
    assert marginal["Crunchyroll"]["count"] == 1
    assert marginal["Crunchyroll"]["titles"] == [{"id": 2, "title": "T2", "status": "WATCHING"}]


def test_setcover_full_ranked_list_has_no_threshold_cutoff(pg_conn, app_module, _seeded_user):
    _seed_realistic_library(pg_conn)

    result = app_module._compute_streaming_setcover(USER_ID)
    ranked = {r["service"]: r for r in result["ranked_all"]}

    # Every STREAMING_SITES entry present, including zero-coverage ones like Hoopla.
    assert set(ranked) == app_module.STREAMING_SITES
    assert ranked["Hoopla"]["count"] == 0
    assert ranked["Hoopla"]["titles"] == []

    # Owned/unowned flag correct.
    assert ranked["Crunchyroll"]["owned"] is True
    assert ranked["Bilibili TV"]["owned"] is False

    # Title-level breakdown, not just a count — this is the exact bilibili gap #255
    # calls out by name. {id, title, status} dicts now (#271) — check both the
    # title set AND that every chip's id is the real matching anime id.
    assert ranked["Bilibili TV"]["count"] == 5
    assert _titles(ranked["Bilibili TV"]["titles"]) == {"T1", "T2", "T3", "T4", "T6"}
    assert {c["id"]: c["title"] for c in ranked["Bilibili TV"]["titles"]} == {
        1: "T1", 2: "T2", 3: "T3", 4: "T4", 6: "T6",
    }

    assert ranked["Crunchyroll"]["count"] == 4
    assert ranked["Hulu"]["count"] == 2
    assert ranked["Netflix"]["count"] == 2


def test_setcover_swap_candidate_appears_when_unowned_matches_owned_total(
    pg_conn, app_module, _seeded_user
):
    _seed_realistic_library(pg_conn)

    result = app_module._compute_streaming_setcover(USER_ID)

    # Bilibili TV (unowned) covers 5, exactly matching owned_total_covered (5) —
    # "matches or beats" per #255's own wording, so it must surface.
    swap_services = {s["service"] for s in result["swap_candidates"]}
    assert "Bilibili TV" in swap_services
    # Max only covers 1 (T6) — nowhere close, must not appear.
    assert "Max" not in swap_services
    assert "Disney Plus" not in swap_services


def test_setcover_swap_candidate_on_exact_match_not_just_strict_beat(
    pg_conn, app_module, _seeded_user
):
    """"Matches OR exceeds" per #255's own wording — an unowned service covering
    exactly the same total as the owned combination (not strictly more) must still
    qualify, since paying for 3 services to get what 1 already gets you is exactly
    the redundant-spend case this feature exists to surface."""
    _insert_anime(pg_conn, 1, ["Crunchyroll", "Netflix", "Hulu", "Max"], title="Everywhere")
    _insert_entry(pg_conn, 1, "WATCHING")
    _set_owned(pg_conn, ["Crunchyroll", "Netflix", "Hulu"])

    result = app_module._compute_streaming_setcover(USER_ID)

    assert result["owned_total_covered"] == 1
    swap_services = {s["service"] for s in result["swap_candidates"]}
    assert swap_services == {"Max"}  # Max alone matches (not beats) the owned total


def test_setcover_no_swap_candidates_when_nothing_covers_anything(pg_conn, app_module, _seeded_user):
    _insert_anime(pg_conn, 1, ["Crunchyroll"], title="Crunchyroll Only")
    _insert_entry(pg_conn, 1, "WATCHING")
    _set_owned(pg_conn, ["Crunchyroll", "Netflix", "Hulu"])

    result = app_module._compute_streaming_setcover(USER_ID)

    assert result["owned_total_covered"] == 1
    # Netflix/Hulu (owned, excluded anyway) and every other unowned service cover 0
    # — none of them qualify as a swap.
    assert result["swap_candidates"] == []


def test_setcover_no_owned_services_produces_no_minimal_combination_or_swap(
    pg_conn, app_module, _seeded_user
):
    """With nothing owned there's no existing combination to recommend keeping or
    swapping out of — the full ranked list is still the right place to look, and
    still must be fully populated (checked separately above)."""
    _insert_anime(pg_conn, 1, ["Netflix"], title="Solo")
    _insert_entry(pg_conn, 1, "PLANNING")

    result = app_module._compute_streaming_setcover(USER_ID)

    assert result["owned"] == []
    assert result["minimal_combination"] == []
    assert result["swap_candidates"] == []
    assert result["owned_total_covered"] == 0


# ── 7. /streaming end-to-end render ─────────────────────────────────────────────

@pytest.fixture()
def client(app_module):
    from starlette.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


def _register_and_login(client, email="owner@example.com", password="correct horse battery staple"):
    resp = client.post(
        "/auth/register",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"registration failed: {resp.text}"


def test_streaming_page_renders_setcover_sections(pg_conn, app_module, client):
    _register_and_login(client)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = 'owner@example.com'")
        (uid,) = cur.fetchone()

    _insert_anime(pg_conn, 1, ["Crunchyroll", "Netflix"], title="Shared Title")
    _insert_entry(pg_conn, 1, "WATCHING", user_id=uid)
    _insert_anime(pg_conn, 2, ["Bilibili TV"], title="Bilibili Exclusive")
    _insert_entry(pg_conn, 2, "PLANNING", user_id=uid)
    _set_owned(pg_conn, ["Crunchyroll"], user_id=uid)

    resp = client.get("/streaming")
    assert resp.status_code == 200
    # The exact bilibili gap #255 names: a title-level breakdown must appear, not
    # just a bare count/percentage.
    assert "Bilibili Exclusive" in resp.text
    assert "Shared Title" in resp.text

    # Nav tab present and marked active on this page.
    assert 'href="/streaming"' in resp.text

    # #271: every title chip now links through to its real /anime/{id}/notes page
    # (id 1 = "Shared Title", id 2 = "Bilibili Exclusive") with the title's own
    # status threaded through as ?back=, not a bare unlinked <span>.
    assert '<a href="/anime/1/notes?back=WATCHING"' in resp.text
    assert '<a href="/anime/2/notes?back=PLANNING"' in resp.text
    assert '<span class="drop-tag-chip">Shared Title</span>' not in resp.text
    assert '<span class="drop-tag-chip">Bilibili Exclusive</span>' not in resp.text
