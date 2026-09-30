"""
Coverage for issue #529 — rows from an earlier recommender run must not be
sorted below every row from the current one.

#186 put presentation order in `diversity_rank` and the read path orders by
`diversity_rank ASC NULLS LAST, score DESC`. But `score_and_store()` only ever
upserted *this run's* candidate set, so rows discovered by an earlier run kept
their old score and never received a rank. With NULLS LAST, that put every one
of them behind every ranked row regardless of score.

Measured on the live instance 2026-09-30: 640 ranked, 606 unranked, and 605 of
the unranked rows outscored the *worst* ranked row — including one scoring a
perfect 100.00, sorted to position 641 on a page that shows 100 per source.

The fix ranks every live row together each run, rather than only the current
candidate set. Simulated against those same 1246 real rows, that puts the
100.00 row at position 1 and raises distinct genres in the top 30 from 7 to 8 —
the MMR pool grows from 640 candidates to 1246, so it has more to diversify
from. Score-banding alternatives were measured too and all collapsed diversity
back to 5 distinct genres, because #186's promoted candidates sit across score
bands rather than within them.

Fake-connection coverage, same discipline as tests/test_run_recommender.py —
no real DB, no network.
"""

import run_recommender as rr


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        q = " ".join(query.split())
        if q.startswith("SELECT id, genres, tags, studios, relations"):
            self._result = self.conn.anime_rows
        elif q.startswith("SELECT rs.anime_id"):
            self._result = self.conn.carried_rows
        elif q.startswith("INSERT INTO recommendation_scores"):
            self.conn.inserted.append(params)
        elif q.startswith("UPDATE recommendation_scores"):
            self.conn.updated.append(params)
        else:
            raise AssertionError(f"unexpected query: {q[:70]!r}")

    def fetchall(self):
        return self._result or []


class _FakeConn:
    def __init__(self, anime_rows, carried_rows):
        self.anime_rows = anime_rows
        self.carried_rows = carried_rows
        self.inserted = []
        self.updated = []

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self)

    def commit(self):
        pass


def _anime(anime_id, genres):
    return {"id": anime_id, "genres": list(genres), "tags": [], "studios": [], "relations": []}


def _carried(anime_id, score, genres):
    """A row already in recommendation_scores that this run did not rescore."""
    return {"anime_id": anime_id, "score": score, "genres": list(genres)}


def _run(monkeypatch, anime_rows, carried_rows, profile):
    monkeypatch.setattr(rr, "fetch_cross_user_signal", lambda conn, ids, uid: {})
    monkeypatch.setattr(rr, "get_library_statuses", lambda conn: {})
    monkeypatch.setattr(rr, "_make_prequel_relation_resolver", lambda conn: (lambda i: []))
    conn = _FakeConn(anime_rows, carried_rows)
    rr.score_and_store(conn, {r["id"] for r in anime_rows}, profile)
    return conn


def _all_ranks(conn):
    """anime_id -> diversity_rank, across both the upserted current-run rows and
    the carried-over rows that only had their rank updated."""
    ranks = {p[1]: p[-1] for p in conn.inserted}
    ranks.update({p[-1]: p[0] for p in conn.updated})
    return ranks


def test_a_carried_over_row_still_receives_a_rank(monkeypatch):
    """THE #529 bug. A row from an earlier run is not in this run's candidate set,
    so before the fix it kept a NULL rank forever and sorted behind everything."""
    profile = {"genres": {"Action": 10.0, "Comedy": 9.0}, "tags": {}, "studios": {}}
    anime_rows = [_anime(301, ["Action"]), _anime(302, ["Action"])]
    carried = [_carried(399, 100.0, ["Comedy"])]

    ranks = _all_ranks(_run(monkeypatch, anime_rows, carried, profile))

    assert 399 in ranks
    assert ranks[399] is not None


# Remaining acceptance criteria. These passed on first run against the
# implementation above — regression guards on just-written behaviour, not
# TDD-driven, except where noted.


def test_a_high_scoring_carried_row_outranks_a_low_scoring_current_row(monkeypatch):
    """The actual user-visible symptom: a 100.00 row sorted to #641, behind a
    1.94 row. Rank order must track relevance across the run boundary, not
    membership of the current candidate set."""
    profile = {"genres": {"Action": 10.0, "Comedy": 9.0}, "tags": {}, "studios": {}}
    anime_rows = [_anime(301, ["Action"]), _anime(302, ["Action"])]
    carried = [_carried(399, 100.0, ["Comedy"])]

    conn = _run(monkeypatch, anime_rows, carried, profile)
    ranks = _all_ranks(conn)

    assert ranks[399] < ranks[302]


def test_carried_rows_have_only_their_rank_written(monkeypatch):
    """A carried row's score/reason/source belong to the run that produced them.
    This run did not rescore it, so it must not rewrite them — the UPDATE touches
    diversity_rank alone, and the row is never re-upserted."""
    profile = {"genres": {"Action": 10.0}, "tags": {}, "studios": {}}
    anime_rows = [_anime(301, ["Action"])]
    carried = [_carried(399, 55.0, ["Comedy"])]

    conn = _run(monkeypatch, anime_rows, carried, profile)

    assert [p[1] for p in conn.inserted] == [301]
    assert [p[-1] for p in conn.updated] == [399]


def test_dismissed_rows_are_not_fetched_for_ranking(monkeypatch):
    """Dismissed rows are filtered on read, so ranking them would let them consume
    diversity slots the user never sees. Asserted via the query the fake accepts:
    the carried-row SELECT must exclude them."""
    import inspect
    source = inspect.getsource(rr.score_and_store)

    assert "NOT rs.dismissed" in source
    assert "NOT (rs.anime_id = ANY(%s))" in source


def test_every_row_current_and_carried_gets_a_distinct_rank(monkeypatch):
    """Ranks must be a contiguous 1..N permutation across BOTH sets — a collision
    would make ordering non-deterministic, and a gap would mean a row was dropped
    from the ranking entirely."""
    profile = {"genres": {"Action": 5.0, "Comedy": 5.0}, "tags": {}, "studios": {}}
    anime_rows = [_anime(300 + i, ["Action"]) for i in range(1, 5)]
    carried = [_carried(400 + i, float(90 - i), ["Comedy"]) for i in range(1, 4)]

    ranks = _all_ranks(_run(monkeypatch, anime_rows, carried, profile))

    assert sorted(ranks.values()) == list(range(1, len(anime_rows) + len(carried) + 1))


def test_no_carried_rows_behaves_exactly_as_before(monkeypatch):
    """A first-ever run, or a run whose candidate set covers everything, must be
    unchanged by #529: current-run rows ranked 1..N, no UPDATE issued at all."""
    profile = {"genres": {"Action": 10.0}, "tags": {}, "studios": {}}
    anime_rows = [_anime(301, ["Action"]), _anime(302, ["Comedy"])]

    conn = _run(monkeypatch, anime_rows, [], profile)

    assert conn.updated == []
    assert sorted(_all_ranks(conn).values()) == [1, 2]
