"""
Coverage for issue #219 — a "liked" flag on personal_notes, separate from the
existing 0-5 star score. Letterboxd's heart-vs-star pattern: a nullable boolean
signal, purely local (never pushed to AniList — the app's only AniList
mutations are rating/status/progress, see CLAUDE.md's guardrail), so a show can
be a 3-star "guilty pleasure I loved" independent of how it's rated.

No real DB is touched: app.main's `db` module functions (fetchall/execute) are
monkeypatched against a tiny in-memory fake modeling `personal_notes`, matching
the pattern used elsewhere in this test suite (test_episode_notes.py,
test_rewatch_notes.py). Tests call _set_favorite directly rather than through
FastAPI's TestClient, since the route (POST /api/anime/{id}/favorite) is a thin
wrapper around it that just adds session-auth + JSON plumbing.
"""

import re
from pathlib import Path

import app.main as main


class FakeDB:
    """Models just enough of `personal_notes` for _set_favorite to run against,
    without a real Postgres connection. Also tracks every other personal_notes
    column so a test can assert a favorite toggle never clobbers them (the
    dedicated-upsert reasoning documented on _set_favorite itself)."""

    def __init__(self, notes=None):
        # {(user_id, anime_id): {"favorite": bool|None, "drop_reason": ..., "notes": ...}}
        self.notes = dict(notes or {})
        self.execute_calls = []

    def execute(self, query, params=None):
        self.execute_calls.append((query, params))
        if "INSERT INTO personal_notes (user_id, anime_id, favorite)" in query:
            user_id, anime_id, favorite = params
            row = self.notes.setdefault((user_id, anime_id), {})
            row["favorite"] = favorite
            return
        raise AssertionError(f"unexpected query: {query}")


def test_set_favorite_true_on_fresh_anime(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)

    main._set_favorite(user_id=1, anime_id=100, favorite=True)

    assert fake.notes[(1, 100)]["favorite"] is True


def test_set_favorite_false_unmarks(monkeypatch):
    fake = FakeDB(notes={(1, 100): {"favorite": True}})
    monkeypatch.setattr(main, "db", fake)

    main._set_favorite(user_id=1, anime_id=100, favorite=False)

    assert fake.notes[(1, 100)]["favorite"] is False


def test_set_favorite_independent_per_anime(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)

    main._set_favorite(user_id=1, anime_id=100, favorite=True)
    main._set_favorite(user_id=1, anime_id=200, favorite=False)

    assert fake.notes[(1, 100)]["favorite"] is True
    assert fake.notes[(1, 200)]["favorite"] is False


def test_set_favorite_independent_per_user(monkeypatch):
    """Two different users favoriting/unfavoriting the same anime_id must not
    collide — personal_notes is keyed UNIQUE (user_id, anime_id)."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)

    main._set_favorite(user_id=1, anime_id=100, favorite=True)
    main._set_favorite(user_id=2, anime_id=100, favorite=False)

    assert fake.notes[(1, 100)]["favorite"] is True
    assert fake.notes[(2, 100)]["favorite"] is False


def test_set_favorite_only_issues_favorite_column_query(monkeypatch):
    """The whole point of _set_favorite being its own dedicated upsert (rather
    than routed through _upsert_personal_notes' full-replace path) is that it
    can never clobber drop_reason/personal_tags/notes/watch_next_priority/
    anilist_id_override set elsewhere. Assert the query it issues only ever
    touches the favorite column."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)

    main._set_favorite(user_id=1, anime_id=100, favorite=True)

    assert len(fake.execute_calls) == 1
    query, params = fake.execute_calls[0]
    set_clause = query.split("DO UPDATE SET", 1)[1]
    assert "favorite" in set_clause
    for other_col in ("drop_reason", "personal_tags", "notes =", "watch_next_priority", "anilist_id_override"):
        assert other_col not in set_clause
    assert params == (1, 100, True)


def test_favorite_column_declared_nullable_boolean_in_schema_and_migration():
    repo_root = Path(__file__).resolve().parent.parent
    schema_text = (repo_root / "schema.sql").read_text(encoding="utf-8")
    migration_text = (repo_root / "migrations" / "023_favorite_flag.sql").read_text(encoding="utf-8")

    assert re.search(r"favorite\s+BOOLEAN", schema_text), "personal_notes.favorite missing from schema.sql"
    assert "NOT NULL" not in re.search(r"favorite\s+BOOLEAN[^,\n]*", schema_text).group(0), (
        "personal_notes.favorite must stay nullable — NULL and FALSE both read as 'not favorited'"
    )
    assert re.search(r"ALTER TABLE personal_notes ADD COLUMN favorite BOOLEAN", migration_text), (
        "migrations/023_favorite_flag.sql missing the additive favorite column"
    )
