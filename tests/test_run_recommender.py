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
