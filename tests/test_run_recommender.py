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


def _run_score_and_store(monkeypatch, anime_rows, library_statuses):
    monkeypatch.setattr(rr, "fetch_cross_user_signal", lambda conn, ids, uid: {})
    monkeypatch.setattr(rr, "get_library_statuses", lambda conn: library_statuses)
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
