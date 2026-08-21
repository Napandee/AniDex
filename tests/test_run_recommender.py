"""
Regression coverage for issue #158 — hide sequel/later-season candidates from the
recommender when an earlier season is already in the user's library but not yet
finished (WATCHING/PLANNING/PAUSED/DROPPED). A COMPLETED earlier season must NOT
suppress the candidate — that's the legitimate "watch next" case the recommender
exists to surface.

Also covers the standing invariant (see CLAUDE.md) that recommender reruns must
never clobber the `dismissed` flag (or `snoozed_until`, issue #75) on existing
recommendation_scores rows — score_and_store()'s ON CONFLICT ... DO UPDATE SET
clause must keep excluding both.

Also covers issue #254 — a candidate's `anime` row used to be treated as
permanently fresh the moment it existed at all, so #158's suppression logic above
silently had nothing to suppress with for any candidate discovered before #158
shipped (real case: Kingdom S2/S4/S5, first discovered 2026-08-10/08-16, `relations`
capture landed 2026-08-19). _select_ids_to_fetch() replaces that binary known/
unknown gate with a staleness-aware one.

No real DB/network touched: fetch_cross_user_signal() and get_library_statuses()
are monkeypatched (same discipline as test_sync_crunchyroll.py's fakes), and a
minimal in-memory stand-in for the psycopg2 connection captures what score_and_store
would have written to `anime` / `recommendation_scores`.
"""

import json

import run_recommender as rr


# ── _has_unwatched_prequel — pure-function unit coverage ─────────────────────

def _media(relations):
    return {"genres": [], "tags": [], "studios": [], "relations": relations}


def test_no_relations_never_suppresses():
    assert rr._has_unwatched_prequel(_media([]), {}) is False


def test_prequel_not_in_library_does_not_suppress():
    media = _media([{"id": 10, "relation_type": "PREQUEL"}])
    assert rr._has_unwatched_prequel(media, {}) is False


def test_non_prequel_relation_type_never_suppresses():
    # e.g. SEQUEL, PARENT, SIDE_STORY — only PREQUEL is in scope for v1.
    media = _media([{"id": 10, "relation_type": "SEQUEL"}])
    assert rr._has_unwatched_prequel(media, {10: "WATCHING"}) is False


def test_prequel_completed_does_not_suppress():
    media = _media([{"id": 10, "relation_type": "PREQUEL"}])
    assert rr._has_unwatched_prequel(media, {10: "COMPLETED"}) is False


def test_prequel_unwatched_statuses_suppress():
    media = _media([{"id": 10, "relation_type": "PREQUEL"}])
    for status in ("WATCHING", "PLANNING", "PAUSED", "DROPPED"):
        assert rr._has_unwatched_prequel(media, {10: status}) is True, status


def test_multiple_relations_only_prequel_checked():
    media = _media([
        {"id": 20, "relation_type": "SIDE_STORY"},
        {"id": 10, "relation_type": "PREQUEL"},
    ])
    assert rr._has_unwatched_prequel(media, {20: "WATCHING", 10: "COMPLETED"}) is False
    assert rr._has_unwatched_prequel(media, {20: "COMPLETED", 10: "WATCHING"}) is True


# ── _has_unwatched_prequel — multi-hop chain walking (issue #266) ────────────
#
# #158's original check only ever looked at the candidate's immediate PREQUEL
# edge. These cover the actual bug: Kingdom S4 was still recommended because its
# direct prequel, S3, was never added to the library at all — the real answer
# (S1 is Paused) was two hops further back. `resolve_relations` here is a plain
# dict-backed stand-in for _make_prequel_relation_resolver's real DB/AniList-
# backed callable — _has_unwatched_prequel only calls it, never builds it itself,
# so this is a faithful unit-level substitute.

KINGDOM_S3_ID = 303
KINGDOM_S4_ID = 404


def _resolver(chain: dict[int, list]):
    """chain: {anime_id: relations-list} — models "this ancestor's own PREQUEL
    edges, as if freshly fetched/loaded from `anime`." Missing keys mean "AniList
    has nothing more for this id" (empty result), matching the real resolver's
    not-found behavior."""
    return lambda anime_id: chain.get(anime_id, [])


def test_multi_hop_chain_with_missing_intermediate_season_suppresses():
    # The exact Kingdom S4 shape: S4 -> S3 (no `anime` row at all, needs an
    # on-demand fetch) -> S2 (same) -> S1 (in the library, Paused).
    candidate = {"id": KINGDOM_S4_ID, "relations": [{"id": KINGDOM_S3_ID, "relation_type": "PREQUEL"}]}
    library_statuses = {KINGDOM_S1_ID: "PAUSED"}
    resolve_relations = _resolver({
        KINGDOM_S3_ID: [{"id": KINGDOM_S2_ID, "relation_type": "PREQUEL"}],
        KINGDOM_S2_ID: [{"id": KINGDOM_S1_ID, "relation_type": "PREQUEL"}],
    })
    assert rr._has_unwatched_prequel(candidate, library_statuses, resolve_relations) is True


def test_multi_hop_chain_everything_watched_does_not_suppress():
    # Two hops back, through an intermediate not in the library, to an ancestor
    # that IS in the library and COMPLETED — the legitimate "watch next" case,
    # unaffected by how many hops away it is.
    candidate = {"id": 900, "relations": [{"id": 901, "relation_type": "PREQUEL"}]}
    library_statuses = {902: "COMPLETED"}
    resolve_relations = _resolver({
        901: [{"id": 902, "relation_type": "PREQUEL"}],
    })
    assert rr._has_unwatched_prequel(candidate, library_statuses, resolve_relations) is False


def test_multi_hop_chain_runs_out_with_no_library_evidence_does_not_suppress():
    # Chain ends (AniList has nothing further) without ever finding an ancestor
    # in the library at all — "no evidence either way," per #266's scope (c).
    candidate = {"id": 910, "relations": [{"id": 911, "relation_type": "PREQUEL"}]}
    resolve_relations = _resolver({911: []})
    assert rr._has_unwatched_prequel(candidate, {}, resolve_relations) is False


def test_prequel_chain_cycle_does_not_infinite_loop():
    # A -> B -> A. Without the visited-set this would loop forever; with it, the
    # walk simply can't requeue A a second time and terminates.
    candidate = {"id": 1, "relations": [{"id": 2, "relation_type": "PREQUEL"}]}
    resolve_relations = _resolver({
        2: [{"id": 1, "relation_type": "PREQUEL"}],  # points back at the candidate
    })
    assert rr._has_unwatched_prequel(candidate, {}, resolve_relations) is False


def test_prequel_chain_self_referencing_cycle_does_not_infinite_loop():
    # A single node whose own PREQUEL edge points at itself.
    candidate = {"id": 1, "relations": [{"id": 1, "relation_type": "PREQUEL"}]}
    resolve_relations = _resolver({1: [{"id": 1, "relation_type": "PREQUEL"}]})
    assert rr._has_unwatched_prequel(candidate, {}, resolve_relations) is False


def test_prequel_chain_depth_cap_bounds_the_walk():
    # A chain longer than MAX_PREQUEL_CHAIN_HOPS, with the one hide-status
    # ancestor sitting just past the cap — the walk must give up before reaching
    # it (bounded worst-case cost) rather than suppressing.
    chain_len = rr.MAX_PREQUEL_CHAIN_HOPS + 5
    chain = {}
    for i in range(1, chain_len + 1):
        chain[i] = [{"id": i + 1, "relation_type": "PREQUEL"}]
    library_statuses = {chain_len + 1: "WATCHING"}  # just out of reach
    candidate = {"id": 0, "relations": [{"id": 1, "relation_type": "PREQUEL"}]}
    resolve_relations = _resolver(chain)
    assert rr._has_unwatched_prequel(candidate, library_statuses, resolve_relations) is False


def test_prequel_chain_within_depth_cap_still_suppresses():
    # Same shape as above, but the hide-status ancestor is well within the cap —
    # proves the cap doesn't accidentally block legitimate shorter real chains.
    chain = {1: [{"id": 2, "relation_type": "PREQUEL"}]}
    library_statuses = {2: "PAUSED"}
    candidate = {"id": 0, "relations": [{"id": 1, "relation_type": "PREQUEL"}]}
    resolve_relations = _resolver(chain)
    assert rr._has_unwatched_prequel(candidate, library_statuses, resolve_relations) is True


def test_single_hop_still_works_without_a_resolver():
    # No resolver given at all (the pre-#266 call shape) — behaves exactly like
    # the original single-hop function; an ancestor not in the library just ends
    # the walk instead of looking further.
    candidate = {"id": 0, "relations": [{"id": 1, "relation_type": "PREQUEL"}]}
    assert rr._has_unwatched_prequel(candidate, {}) is False
    assert rr._has_unwatched_prequel(candidate, {1: "WATCHING"}) is True


# ── _make_prequel_relation_resolver — on-demand ancestor lookups (issue #266) ─
#
# The real resolver _has_unwatched_prequel's chain walk uses in production:
# local `anime` row first, live AniList fetch (same MEDIA_DETAILS_QUERY/gql()/
# _upsert_anime path as everywhere else in this file) only when that row is
# missing or has never captured relations, in-run memoization either way.

class _FakeResolverCursor:
    def __init__(self, conn):
        self.conn = conn
        self._select_id = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        q = query.strip()
        if q.startswith("SELECT relations FROM anime WHERE id"):
            self._select_id = params[0]
        elif q.startswith("INSERT INTO anime"):
            self.conn.upserted_ids.append(params[0])  # media["id"] is the first bind param
        else:
            raise AssertionError(f"unexpected query in fake resolver cursor: {q[:60]!r}")

    def fetchone(self):
        row = self.conn.db_rows.get(self._select_id)
        return {"relations": row} if row is not None else None


class _FakeResolverConn:
    """db_rows: {anime_id: relations-list} standing in for what's already in the
    `anime` table — an id absent from this dict models "no row at all"."""

    def __init__(self, db_rows):
        self.db_rows = db_rows
        self.upserted_ids: list[int] = []
        self.committed = 0

    def cursor(self, cursor_factory=None):
        return _FakeResolverCursor(self)

    def commit(self):
        self.committed += 1


def _anilist_media_payload(media_id, prequel_id=None, prequel_title="Prequel"):
    edges = []
    if prequel_id is not None:
        edges.append({
            "relationType": "PREQUEL",
            "node": {
                "id": prequel_id,
                "title": {"romaji": prequel_title, "english": None},
                "coverImage": {"large": None},
                "format": "TV",
            },
        })
    return {
        "Page": {
            "media": [{
                "id": media_id,
                "idMal": None,
                "title": {"romaji": f"Show {media_id}", "english": None, "native": None},
                "format": "TV", "status": "FINISHED", "episodes": 13,
                "season": "SPRING", "seasonYear": 2021,
                "genres": [], "tags": [], "studios": {"edges": []},
                "averageScore": None, "coverImage": {}, "bannerImage": None, "description": None,
                "externalLinks": [], "streamingEpisodes": [],
                "relations": {"edges": edges},
            }],
        },
    }


def test_relation_resolver_uses_local_row_without_hitting_anilist(monkeypatch):
    conn = _FakeResolverConn(db_rows={KINGDOM_S3_ID: [{"id": KINGDOM_S1_ID, "relation_type": "PREQUEL"}]})

    def fail_if_called(*a, **kw):
        raise AssertionError("must not hit AniList when a local row already has relations")

    monkeypatch.setattr(rr, "gql", fail_if_called)

    resolve_relations = rr._make_prequel_relation_resolver(conn)
    assert resolve_relations(KINGDOM_S3_ID) == [{"id": KINGDOM_S1_ID, "relation_type": "PREQUEL"}]


def test_relation_resolver_fetches_from_anilist_when_row_missing(monkeypatch):
    conn = _FakeResolverConn(db_rows={})
    call_ids = []

    def fake_gql(query, variables, retries=5):
        call_ids.append(list(variables["ids"]))
        return _anilist_media_payload(KINGDOM_S3_ID, prequel_id=KINGDOM_S2_ID, prequel_title="Kingdom 2nd Season")

    monkeypatch.setattr(rr, "gql", fake_gql)
    monkeypatch.setattr(rr.time, "sleep", lambda *_: None)

    resolve_relations = rr._make_prequel_relation_resolver(conn)
    result = resolve_relations(KINGDOM_S3_ID)

    assert result == [{
        "id": KINGDOM_S2_ID,
        "title": "Kingdom 2nd Season",
        "cover": None,
        "format": "TV",
        "relation_type": "PREQUEL",
    }]
    assert call_ids == [[KINGDOM_S3_ID]]
    assert conn.upserted_ids == [KINGDOM_S3_ID]  # persisted so future runs don't refetch it
    assert conn.committed == 1


def test_relation_resolver_memoizes_within_one_run(monkeypatch):
    # Same ancestor needed twice in one run (e.g. two candidates sharing a
    # franchise) must only ever hit AniList once.
    conn = _FakeResolverConn(db_rows={})
    call_ids = []

    def fake_gql(query, variables, retries=5):
        call_ids.append(list(variables["ids"]))
        return _anilist_media_payload(KINGDOM_S3_ID)

    monkeypatch.setattr(rr, "gql", fake_gql)
    monkeypatch.setattr(rr.time, "sleep", lambda *_: None)

    resolve_relations = rr._make_prequel_relation_resolver(conn)
    first = resolve_relations(KINGDOM_S3_ID)
    second = resolve_relations(KINGDOM_S3_ID)

    assert first == second == []
    assert call_ids == [[KINGDOM_S3_ID]]  # only one AniList call across both lookups


def test_relation_resolver_handles_anilist_returning_no_media(monkeypatch):
    conn = _FakeResolverConn(db_rows={})
    monkeypatch.setattr(rr, "gql", lambda *a, **kw: {"Page": {"media": []}})
    monkeypatch.setattr(rr.time, "sleep", lambda *_: None)

    resolve_relations = rr._make_prequel_relation_resolver(conn)
    assert resolve_relations(999999) == []


def test_relation_resolver_handles_anilist_error_without_raising(monkeypatch):
    # A transient AniList failure mid-chain-walk must not blow up the whole
    # recommender run — treated as "no evidence," same as a genuine dead end.
    conn = _FakeResolverConn(db_rows={})

    def boom(*a, **kw):
        raise RuntimeError("AniList unreachable")

    monkeypatch.setattr(rr, "gql", boom)

    resolve_relations = rr._make_prequel_relation_resolver(conn)
    assert resolve_relations(123) == []


# ── _upsert_anime — relations persistence (mirrors sync_anilist.py's pattern) ─

class _FakeUpsertCursor:
    def __init__(self):
        self.last_params = None

    def execute(self, query, params):
        self.last_params = params


def test_upsert_anime_persists_relations():
    media = {
        "id": 999,
        "idMal": None,
        "title": {"romaji": "Some Show S4", "english": None, "native": None},
        "format": "TV",
        "status": "FINISHED",
        "episodes": 12,
        "season": "FALL",
        "seasonYear": 2025,
        "genres": [],
        "tags": [],
        "studios": {"edges": []},
        "averageScore": None,
        "coverImage": {},
        "bannerImage": None,
        "description": None,
        "externalLinks": [],
        "streamingEpisodes": [],
        "relations": {
            "edges": [
                {
                    "relationType": "PREQUEL",
                    "node": {
                        "id": 111,
                        "title": {"romaji": "Some Show S3", "english": None},
                        "coverImage": {"large": "http://example/cover.jpg"},
                        "format": "TV",
                    },
                },
            ]
        },
    }
    cur = _FakeUpsertCursor()
    rr._upsert_anime(cur, media)

    relations_param = json.loads(cur.last_params[-1])
    assert relations_param == [
        {
            "id": 111,
            "title": "Some Show S3",
            "cover": "http://example/cover.jpg",
            "format": "TV",
            "relation_type": "PREQUEL",
        }
    ]


def test_upsert_anime_defaults_relations_to_empty_list_when_absent():
    media = {
        "id": 998,
        "idMal": None,
        "title": {"romaji": "No Relations Show", "english": None, "native": None},
        "format": "TV",
        "status": "FINISHED",
        "episodes": 12,
        "season": "FALL",
        "seasonYear": 2025,
        "genres": [],
        "tags": [],
        "studios": {"edges": []},
        "averageScore": None,
        "coverImage": {},
        "bannerImage": None,
        "description": None,
        "externalLinks": [],
        "streamingEpisodes": [],
        # no "relations" key at all — AniList sometimes omits it
    }
    cur = _FakeUpsertCursor()
    rr._upsert_anime(cur, media)
    assert json.loads(cur.last_params[-1]) == []


# ── _select_ids_to_fetch — staleness-aware candidate refresh gate (issue #254) ──

class _FakeStalenessCursor:
    """Stand-in for the single `SELECT id FROM anime WHERE ... last_synced_at > ...`
    query _select_ids_to_fetch issues. `fresh_ids` is the canned set the fake "DB"
    would return — i.e. rows that exist AND are within the staleness window."""

    def __init__(self, fresh_ids):
        self.fresh_ids = fresh_ids
        self.last_query = None
        self.last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params):
        self.last_query = query
        self.last_params = params

    def fetchall(self):
        return [(i,) for i in self.fresh_ids]


class _FakeStalenessConn:
    def __init__(self, fresh_ids):
        self.cur = _FakeStalenessCursor(fresh_ids)

    def cursor(self):
        return self.cur


def test_select_ids_to_fetch_refetches_unknown_ids():
    # id 1 has never been seen at all — no row in `anime` — must be selected.
    conn = _FakeStalenessConn(fresh_ids=set())
    assert rr._select_ids_to_fetch(conn, {1}) == {1}


def test_select_ids_to_fetch_skips_genuinely_fresh_known_ids():
    # id 1 exists and is within the staleness window — the caching behavior this
    # replaces must still be preserved for candidates that haven't gone stale.
    conn = _FakeStalenessConn(fresh_ids={1})
    assert rr._select_ids_to_fetch(conn, {1}) == set()


def test_select_ids_to_fetch_refetches_stale_known_ids():
    # id 1 has a row, but it's older than the staleness window (the fake DB's
    # canned "fresh" query simply doesn't return it) — this is the exact Kingdom
    # S2 case: a row that exists but predates a feature that depends on newer data.
    conn = _FakeStalenessConn(fresh_ids=set())
    assert rr._select_ids_to_fetch(conn, {1}) == {1}


def test_select_ids_to_fetch_mixed_batch_only_refetches_unknown_and_stale():
    conn = _FakeStalenessConn(fresh_ids={2})  # only 2 is known-and-fresh
    assert rr._select_ids_to_fetch(conn, {1, 2, 3}) == {1, 3}


def test_select_ids_to_fetch_empty_candidates_short_circuits_without_querying():
    conn = _FakeStalenessConn(fresh_ids=set())
    assert rr._select_ids_to_fetch(conn, set()) == set()
    assert conn.cur.last_query is None  # never even ran a query for an empty set


def test_select_ids_to_fetch_uses_configured_staleness_window():
    conn = _FakeStalenessConn(fresh_ids=set())
    rr._select_ids_to_fetch(conn, {1}, staleness_days=30)
    assert conn.cur.last_params[1] == 30


# ── Kingdom S2 reproduction — stale/empty relations self-heals end to end ─────
#
# Shape of the real bug (issue #254): Kingdom S2 was discovered as a recommender
# candidate before #158's relations-capture code shipped, so its `anime` row has
# been sitting with `relations = []` and a stale `last_synced_at` ever since —
# even though it already has a real row (not "unknown"). #158's suppression logic
# is correct; the data it depends on just never got refreshed. This walks the
# full lifecycle: (1) the stale row gets selected for refetch, (2) refetching
# repopulates `relations` with the real PREQUEL edge, (3) scoring against that
# now-fresh data correctly suppresses the candidate.

KINGDOM_S1_ID = 101
KINGDOM_S2_ID = 202


def test_kingdom_s2_stale_row_is_selected_for_refetch():
    # Kingdom S2's row exists but is not in the "fresh" set the staleness query
    # would return — mirrors last_synced_at being older than CANDIDATE_STALENESS_DAYS.
    conn = _FakeStalenessConn(fresh_ids=set())
    assert rr._select_ids_to_fetch(conn, {KINGDOM_S2_ID}) == {KINGDOM_S2_ID}


def test_kingdom_s2_refetch_repopulates_relations():
    # Before the fix, this candidate would never be re-fetched at all, so
    # `relations` would stay permanently `[]`. After the fix, fetch_and_store_candidates
    # is called on it and _upsert_anime persists the real PREQUEL edge.
    media = {
        "id": KINGDOM_S2_ID,
        "idMal": None,
        "title": {"romaji": "Kingdom 2nd Season", "english": None, "native": None},
        "format": "TV", "status": "FINISHED", "episodes": 13,
        "season": "SPRING", "seasonYear": 2019,
        "genres": [], "tags": [], "studios": {"edges": []},
        "averageScore": None, "coverImage": {}, "bannerImage": None, "description": None,
        "externalLinks": [], "streamingEpisodes": [],
        "relations": {
            "edges": [
                {
                    "relationType": "PREQUEL",
                    "node": {
                        "id": KINGDOM_S1_ID,
                        "title": {"romaji": "Kingdom", "english": None},
                        "coverImage": {"large": "http://example/kingdom-s1.jpg"},
                        "format": "TV",
                    },
                },
            ]
        },
    }
    cur = _FakeUpsertCursor()
    rr._upsert_anime(cur, media)

    relations_param = json.loads(cur.last_params[-1])
    assert relations_param == [
        {
            "id": KINGDOM_S1_ID,
            "title": "Kingdom",
            "cover": "http://example/kingdom-s1.jpg",
            "format": "TV",
            "relation_type": "PREQUEL",
        }
    ]


def test_kingdom_s2_no_longer_recommended_once_relations_refreshed(monkeypatch):
    # Kingdom S1 is PAUSED in the library (Andreas's real reported case) — once
    # S2's relations are populated (simulating the post-refetch state), scoring
    # must suppress it exactly like any other candidate with a fresh PREQUEL edge.
    anime_rows = [
        _anime_row(KINGDOM_S2_ID, relations=[{"id": KINGDOM_S1_ID, "relation_type": "PREQUEL"}]),
    ]
    conn, n = _run_score_and_store(monkeypatch, anime_rows, {KINGDOM_S1_ID: "PAUSED"})
    assert n == 0
    assert conn.inserted == []


# ── score_and_store — end-to-end suppression + dismissed-flag preservation ───

class _FakeScoreCursor:
    """Minimal psycopg2-cursor stand-in for score_and_store()'s two `with
    conn.cursor(...) as cur:` blocks: one SELECT against `anime`, one loop of
    INSERT ... ON CONFLICT against `recommendation_scores`."""

    def __init__(self, conn):
        self.conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        q = query.strip()
        if q.startswith("SELECT id, genres, tags, studios, relations"):
            self._result = self.conn.anime_rows
        elif q.startswith("INSERT INTO recommendation_scores"):
            self.conn.inserted.append({"query": q, "params": params})
        else:
            raise AssertionError(f"unexpected query in fake cursor: {q[:60]!r}")

    def fetchall(self):
        return self._result or []


class _FakeScoreConn:
    def __init__(self, anime_rows):
        self.anime_rows = anime_rows
        self.inserted = []
        self.committed = False

    def cursor(self, cursor_factory=None):
        return _FakeScoreCursor(self)

    def commit(self):
        self.committed = True


def _anime_row(anime_id, relations=None):
    return {
        "id": anime_id,
        "genres": [],
        "tags": [],
        "studios": [],
        "relations": relations or [],
    }


def _run_score_and_store(monkeypatch, anime_rows, library_statuses, resolve_relations=None):
    """`resolve_relations`, if given, is a plain anime_id -> relations-list
    callable standing in for _make_prequel_relation_resolver's real DB/AniList-
    backed one (issue #266) — score_and_store() only ever calls the factory, not
    a DB/network client directly, so monkeypatching the factory to hand back a
    fixed callable is enough to exercise the multi-hop wiring without needing the
    fake cursor/conn below to understand the resolver's own queries. Defaults to
    "nothing more to find" (an always-empty resolver), which reproduces the exact
    pre-#266 single-hop behavior for every test that isn't specifically about
    walking past the immediate prequel."""
    monkeypatch.setattr(rr, "fetch_cross_user_signal", lambda conn, ids, uid: {})
    monkeypatch.setattr(rr, "get_library_statuses", lambda conn: library_statuses)
    monkeypatch.setattr(
        rr, "_make_prequel_relation_resolver",
        lambda conn: (resolve_relations or (lambda anime_id: [])),
    )
    conn = _FakeScoreConn(anime_rows)
    profile = {"genres": {}, "tags": {}, "studios": {}}
    ids = {row["id"] for row in anime_rows}
    n = rr.score_and_store(conn, ids, profile)
    return conn, n


def test_prequel_in_library_unwatched_statuses_suppress_candidate(monkeypatch):
    for status in ("WATCHING", "PLANNING", "PAUSED", "DROPPED"):
        anime_rows = [
            _anime_row(500, relations=[{"id": 10, "relation_type": "PREQUEL"}]),
        ]
        conn, n = _run_score_and_store(monkeypatch, anime_rows, {10: status})
        assert n == 0, status
        assert conn.inserted == [], status


def test_prequel_in_library_completed_does_not_suppress_candidate(monkeypatch):
    anime_rows = [
        _anime_row(500, relations=[{"id": 10, "relation_type": "PREQUEL"}]),
    ]
    conn, n = _run_score_and_store(monkeypatch, anime_rows, {10: "COMPLETED"})
    assert n == 1
    assert len(conn.inserted) == 1
    assert conn.inserted[0]["params"][1] == 500  # anime_id


def test_candidate_with_no_prequel_relation_unaffected(monkeypatch):
    anime_rows = [_anime_row(501, relations=[])]
    conn, n = _run_score_and_store(monkeypatch, anime_rows, {10: "WATCHING"})
    assert n == 1
    assert len(conn.inserted) == 1
    assert conn.inserted[0]["params"][1] == 501


def test_candidate_with_no_owned_earlier_season_unaffected(monkeypatch):
    # PREQUEL relation exists, but the target isn't in the library at all.
    anime_rows = [
        _anime_row(502, relations=[{"id": 999, "relation_type": "PREQUEL"}]),
    ]
    conn, n = _run_score_and_store(monkeypatch, anime_rows, {})
    assert n == 1
    assert conn.inserted[0]["params"][1] == 502


def test_mixed_batch_only_suppresses_the_sequel_candidate(monkeypatch):
    anime_rows = [
        _anime_row(500, relations=[{"id": 10, "relation_type": "PREQUEL"}]),  # suppressed
        _anime_row(501, relations=[]),  # unaffected
    ]
    conn, n = _run_score_and_store(monkeypatch, anime_rows, {10: "WATCHING"})
    assert n == 1
    stored_ids = {row["params"][1] for row in conn.inserted}
    assert stored_ids == {501}


def test_score_and_store_suppresses_via_multi_hop_chain(monkeypatch):
    # Integration-level proof that score_and_store() actually wires a resolver
    # through to _has_unwatched_prequel (issue #266) — not just that the pure
    # function can walk a chain when handed one directly (covered above). The
    # candidate's direct prequel (S3) isn't itself in the library; the resolver
    # supplies S3's own PREQUEL edge to S2, and S2 -> S1 is in the library, Paused.
    anime_rows = [
        _anime_row(KINGDOM_S4_ID, relations=[{"id": KINGDOM_S3_ID, "relation_type": "PREQUEL"}]),
    ]
    resolve_relations = _resolver({KINGDOM_S3_ID: [{"id": KINGDOM_S1_ID, "relation_type": "PREQUEL"}]})
    conn, n = _run_score_and_store(
        monkeypatch, anime_rows, {KINGDOM_S1_ID: "PAUSED"}, resolve_relations=resolve_relations
    )
    assert n == 0
    assert conn.inserted == []


# ── AniList vote count plumbing (issue #226) ──────────────────────────────────

def test_fetch_recommendation_candidates_captures_vote_counts(monkeypatch):
    def fake_gql(query, variables, retries=5):
        return {
            "Media": {
                "recommendations": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "rating": 1204,
                            "mediaRecommendation": {"id": 700, "type": "ANIME", "title": {"romaji": "X"}},
                        },
                        {
                            # No votes yet — must not show up in vote_counts at all.
                            "rating": 0,
                            "mediaRecommendation": {"id": 701, "type": "ANIME", "title": {"romaji": "Y"}},
                        },
                        {
                            # Net-negative — dropped, "N users agree" doesn't read sensibly.
                            "rating": -3,
                            "mediaRecommendation": {"id": 702, "type": "ANIME", "title": {"romaji": "Z"}},
                        },
                        {
                            # Already in the library — excluded from candidates entirely.
                            "rating": 500,
                            "mediaRecommendation": {"id": 703, "type": "ANIME", "title": {"romaji": "W"}},
                        },
                        {
                            # A MANGA recommendation — excluded, same as before this issue.
                            "rating": 999,
                            "mediaRecommendation": {"id": 704, "type": "MANGA", "title": {"romaji": "M"}},
                        },
                    ],
                }
            }
        }

    import run_recommender as rr

    monkeypatch.setattr(rr, "gql", fake_gql)
    monkeypatch.setattr(rr.time, "sleep", lambda *_: None)

    candidates, vote_counts = rr.fetch_recommendation_candidates([1], library_ids={703})

    assert candidates == {700, 701, 702}
    assert vote_counts == {700: 1204}


def test_fetch_recommendation_candidates_keeps_max_rating_across_shows(monkeypatch):
    import run_recommender as rr

    calls = {"n": 0}

    def fake_gql(query, variables, retries=5):
        calls["n"] += 1
        rating = 50 if variables["mediaId"] == 1 else 900
        return {
            "Media": {
                "recommendations": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "rating": rating,
                            "mediaRecommendation": {"id": 800, "type": "ANIME", "title": {"romaji": "Shared"}},
                        }
                    ],
                }
            }
        }

    monkeypatch.setattr(rr, "gql", fake_gql)
    monkeypatch.setattr(rr.time, "sleep", lambda *_: None)

    candidates, vote_counts = rr.fetch_recommendation_candidates([1, 2], library_ids=set())

    assert candidates == {800}
    assert vote_counts == {800: 900}  # strongest of the two edges, not the first or last seen


def test_score_candidate_carries_anilist_vote_count_only_when_given():
    import run_recommender as rr

    media = {"genres": [], "tags": [], "studios": []}
    profile = {"genres": {}, "tags": {}, "studios": {}}

    _, reason_with = rr.score_candidate(media, profile, anilist_vote_count=1204)
    assert reason_with["anilist_vote_count"] == 1204

    _, reason_without = rr.score_candidate(media, profile)
    assert reason_without["anilist_vote_count"] is None


def test_score_and_store_writes_anilist_vote_count_into_reason(monkeypatch):
    anime_rows = [_anime_row(500), _anime_row(501)]
    monkeypatch.setattr(rr, "fetch_cross_user_signal", lambda conn, ids, uid: {})
    monkeypatch.setattr(rr, "get_library_statuses", lambda conn: {})
    conn = _FakeScoreConn(anime_rows)
    profile = {"genres": {}, "tags": {}, "studios": {}}
    ids = {500, 501}

    n = rr.score_and_store(conn, ids, profile, vote_counts={500: 1204})

    assert n == 2
    reasons = {row["params"][1]: json.loads(row["params"][3]) for row in conn.inserted}
    assert reasons[500]["anilist_vote_count"] == 1204
    # 501 was never seen in the recommendations edge — no fabricated count.
    assert reasons[501]["anilist_vote_count"] is None


def test_dismissed_and_snoozed_excluded_from_update_set(monkeypatch):
    # Standing invariant (CLAUDE.md): recommender reruns must never clobber a
    # user's dismissed/snoozed decision on an existing recommendation_scores row.
    anime_rows = [_anime_row(501, relations=[])]
    conn, n = _run_score_and_store(monkeypatch, anime_rows, {})
    assert n == 1
    query = conn.inserted[0]["query"]
    set_clause = query.split("DO UPDATE SET", 1)[1]
    # Strip SQL comment lines (`-- ...`) before checking — dismissed/snoozed_until
    # are mentioned there explaining *why* they're excluded, which isn't the same
    # as them actually being assigned in the SET clause.
    code_only = "\n".join(
        line for line in set_clause.splitlines() if not line.strip().startswith("--")
    )
    assert "dismissed" not in code_only
    assert "snoozed_until" not in code_only
