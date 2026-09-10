"""
Coverage for issue #186 — MMR-style diversity re-rank so the recommender's top-N
isn't one genre cluster.

Measured 2026-09-10 against the live instance: the top 30 recommendations were
25/30 Fantasy and 25/30 Action, with only 5 distinct genres across all 30 slots,
while the library's own completed spread includes Comedy (42), Romance (31),
Ecchi (18), Mystery (11), Supernatural (11), Mecha (9) and Psychological (6) —
none of which surfaced at all.

The hard invariant here is that `score` is NOT touched. The recommend->outcome
hit-rate (issue #185) is measured off `score`, and the baseline this change is
validated against (22 hits / 1173 eligible = 1.88%) was captured on that column.
Diluting `score` with a diversity term would both invalidate that baseline and
desynchronise `score` from the `reason` JSONB that explains it. Ordering
therefore lives in its own `diversity_rank` column.

Pure-function coverage — no DB, no network.
"""

import run_recommender as rr


def _media(genres, tags=None):
    return {"genres": list(genres), "tags": list(tags or []), "studios": []}


def test_unrepresented_genre_outranks_higher_scoring_duplicate():
    """The whole point: once Action/Fantasy is represented, a lower-scoring
    candidate from an unrepresented genre must come before another Action/Fantasy
    one. Without a diversity term, rank order is just score order and id 2 wins."""
    scored = [
        (1, 100.0, {}),
        (2, 95.0, {}),
        (3, 90.0, {}),
    ]
    media = {
        1: _media(["Action", "Fantasy"]),
        2: _media(["Action", "Fantasy"]),
        3: _media(["Comedy"]),
    }

    ranks = rr._diversity_rerank(scored, media)

    assert ranks[3] < ranks[2]


def test_redundancy_does_not_decay_as_the_selected_set_grows():
    """Redundancy must be measured against the MOST SIMILAR single selected item,
    not against the accumulated union of everything selected so far.

    Union-Jaccard looks equivalent on a 3-item toy case and is not: once several
    genres are represented, a candidate that exactly duplicates an early pick
    scores only |intersection| / |union| — 1/5 here — so the penalty shrinks the
    more diverse the list already is, exactly when it should not. Simulated
    against the real 1173-candidate production set, that formulation moved
    distinct genres in the top 30 from 5 to 6. Useless.

    Here: id 6 duplicates id 1 exactly but scores 95; id 7 brings an unseen genre
    at only 70. Under union-Jaccard id 6's penalty is 1/5*0.3 = 0.06, leaving it
    ahead (0.605 vs 0.490) — under max-pairwise its penalty is the full 0.3,
    putting id 7 ahead (0.365 vs 0.490). The assertion below separates them.
    """
    scored = [
        (1, 100.0, {}), (2, 99.0, {}), (3, 98.0, {}), (4, 97.0, {}), (5, 96.0, {}),
        (6, 95.0, {}),
        (7, 70.0, {}),
    ]
    media = {
        1: _media(["A"]), 2: _media(["B"]), 3: _media(["C"]),
        4: _media(["D"]), 5: _media(["E"]),
        6: _media(["A"]),
        7: _media(["F"]),
    }

    ranks = rr._diversity_rerank(scored, media)

    assert ranks[7] < ranks[6]


# ── score_and_store persistence ─────────────────────────────────────────────
# Same fake-connection discipline as tests/test_run_recommender.py: no real DB,
# no network. The contract asserted here is that diversity_rank is persisted as
# the LAST bound parameter, and that `score` is untouched by the re-rank.

class _FakeCursor:
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
            self.conn.inserted.append(params)
        else:
            raise AssertionError(f"unexpected query: {q[:60]!r}")

    def fetchall(self):
        return self._result or []


class _FakeConn:
    def __init__(self, anime_rows):
        self.anime_rows = anime_rows
        self.inserted = []

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self)

    def commit(self):
        pass


def _row(anime_id, genres):
    return {
        "id": anime_id,
        "genres": list(genres),
        "tags": [],
        "studios": [],
        "relations": [],
    }


def _run(monkeypatch, anime_rows, profile):
    monkeypatch.setattr(rr, "fetch_cross_user_signal", lambda conn, ids, uid: {})
    monkeypatch.setattr(rr, "get_library_statuses", lambda conn: {})
    monkeypatch.setattr(rr, "_make_prequel_relation_resolver", lambda conn: (lambda i: []))
    conn = _FakeConn(anime_rows)
    rr.score_and_store(conn, {r["id"] for r in anime_rows}, profile)
    return conn


def _ranks(conn):
    # params tuple ends with the diversity rank; anime_id is the 2nd element
    return {p[1]: p[-1] for p in conn.inserted}


def test_score_and_store_persists_diversity_rank_breaking_the_cluster(monkeypatch):
    """Two Action/Fantasy candidates score above a lone Comedy one. Ranked purely
    by score the Comedy candidate is last; the persisted diversity_rank must put
    it ahead of the second Action/Fantasy candidate instead."""
    profile = {
        "genres": {"Action": 10.0, "Fantasy": 10.0, "Comedy": 8.0},
        "tags": {},
        "studios": {},
    }
    rows = [
        _row(1, ["Action", "Fantasy"]),
        _row(2, ["Action", "Fantasy"]),
        _row(3, ["Comedy"]),
    ]

    ranks = _ranks(_run(monkeypatch, rows, profile))

    assert ranks[1] == 1
    assert ranks[3] < ranks[2]


def test_score_column_is_untouched_by_the_rerank(monkeypatch):
    """THE hard constraint. `score` anchors the #185 hit-rate metric and the UI's
    match percentage; the 1.88% baseline this change is measured against was
    captured on it. The re-rank must move ORDER only.

    Regression guard, not a TDD-driven test — it passed the moment it was
    written, because the implementation never touched `score`. It exists so a
    later 'simplification' that folds the diversity term back into `score`
    fails loudly instead of silently invalidating the baseline.
    """
    profile = {
        "genres": {"Action": 10.0, "Fantasy": 10.0, "Comedy": 8.0},
        "tags": {},
        "studios": {},
    }
    rows = [
        _row(1, ["Action", "Fantasy"]),
        _row(2, ["Action", "Fantasy"]),
        _row(3, ["Comedy"]),
    ]

    conn = _run(monkeypatch, rows, profile)
    scores = {p[1]: p[2] for p in conn.inserted}
    ranks = _ranks(conn)

    # Normalisation invariant survives: the best-scoring candidate is still 100.
    assert max(scores.values()) == 100.0
    # id 3 is promoted by the re-rank, yet its score stays below id 2's — proof
    # the promotion lives in diversity_rank and not in a doctored score.
    assert ranks[3] < ranks[2]
    assert scores[3] < scores[2]


def test_every_candidate_receives_exactly_one_rank(monkeypatch):
    """A greedy pop-loop is exactly the shape that silently drops or duplicates
    entries. Ranks must be a contiguous 1..N permutation."""
    profile = {"genres": {"Action": 5.0}, "tags": {}, "studios": {}}
    rows = [_row(i, ["Action"] if i % 2 else ["Comedy"]) for i in range(1, 8)]

    ranks = _ranks(_run(monkeypatch, rows, profile))

    assert sorted(ranks.values()) == list(range(1, len(rows) + 1))
