"""
Coverage for issue #14 — attaching a note to a specific rewatch, keyed to
library_entries.repeat_count, without touching the general/original note on
personal_notes.notes.

No real DB is touched: app.main's `db` module functions (fetchall/execute) are
monkeypatched against a tiny in-memory fake modeling `library_entries` and
`rewatch_notes`, matching the pattern used elsewhere in this test suite (see
test_import_personal_notes.py) of monkeypatching the handful of DB-touching calls
rather than standing up a real Postgres. Tests call the route helpers
(_get_rewatch_notes / _save_rewatch_note) directly rather than through
FastAPI's TestClient, since the routes themselves are thin wrappers around these
that just add session-auth plumbing.

Covers the three scenarios called out in the task:
- adding a rewatch-specific note doesn't touch the general note
- an anime with no rewatches behaves exactly as before (regression check)
- multiple rewatch notes for the same anime are stored/retrieved correctly per
  rewatch number
"""

import app.main as main


class FakeDB:
    """Models just enough of `library_entries` and `rewatch_notes` for
    _get_rewatch_notes / _save_rewatch_note to run against, without a real
    Postgres connection."""

    def __init__(self, repeat_counts=None, rewatch_notes=None):
        # {(user_id, anime_id): repeat_count}
        self.repeat_counts = dict(repeat_counts or {})
        # {(user_id, anime_id, repeat_count): note}
        self.rewatch_notes = dict(rewatch_notes or {})
        self.execute_calls = []

    def fetchone(self, query, params=None):
        if "FROM library_entries" in query:
            anime_id, user_id = params
            rc = self.repeat_counts.get((user_id, anime_id))
            return {"repeat_count": rc} if rc is not None else None
        raise AssertionError(f"unexpected query: {query}")

    def fetchall(self, query, params=None):
        if "FROM rewatch_notes" in query:
            anime_id, user_id = params
            return [
                {"repeat_count": rc, "note": note}
                for (uid, aid, rc), note in sorted(self.rewatch_notes.items())
                if uid == user_id and aid == anime_id
            ]
        raise AssertionError(f"unexpected query: {query}")

    def execute(self, query, params=None):
        self.execute_calls.append((query, params))
        if "INSERT INTO rewatch_notes" in query:
            user_id, anime_id, repeat_count, note = params
            self.rewatch_notes[(user_id, anime_id, repeat_count)] = note
        elif "DELETE FROM rewatch_notes" in query:
            user_id, anime_id, repeat_count = params
            self.rewatch_notes.pop((user_id, anime_id, repeat_count), None)
        else:
            raise AssertionError(f"unexpected query: {query}")


def test_no_rewatches_returns_empty_list(monkeypatch):
    """Regression check: an anime with repeat_count 0 (never rewatched) gets no
    rewatch-note rows at all — existing single-note behavior is unaffected."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)

    result = main._get_rewatch_notes(user_id=1, anime_id=100, current_repeat_count=0)

    assert result == []


def test_save_rewatch_note_does_not_touch_general_note(monkeypatch):
    """Adding a rewatch-specific note only writes to rewatch_notes, never to
    personal_notes — verified here by asserting no personal_notes query is ever
    issued against the fake DB (it would raise AssertionError if one were)."""
    fake = FakeDB(repeat_counts={(1, 100): 1})
    monkeypatch.setattr(main, "db", fake)

    applied = main._save_rewatch_note(user_id=1, anime_id=100, repeat_count=1, note="hit different")

    assert applied is True
    assert fake.rewatch_notes[(1, 100, 1)] == "hit different"
    # Only rewatch_notes queries were issued — no personal_notes touch.
    for query, _ in fake.execute_calls:
        assert "personal_notes" not in query


def test_multiple_rewatch_notes_stored_and_retrieved_per_number(monkeypatch):
    """Notes for rewatch #1 and rewatch #2 of the same anime are independent —
    saving one doesn't clobber the other, and retrieval returns both correctly
    keyed by repeat_count."""
    fake = FakeDB(repeat_counts={(1, 100): 2})
    monkeypatch.setattr(main, "db", fake)

    assert main._save_rewatch_note(user_id=1, anime_id=100, repeat_count=1, note="first rewatch note")
    assert main._save_rewatch_note(user_id=1, anime_id=100, repeat_count=2, note="second rewatch note")

    result = main._get_rewatch_notes(user_id=1, anime_id=100, current_repeat_count=2)

    assert result == [
        {"repeat_count": 1, "note": "first rewatch note"},
        {"repeat_count": 2, "note": "second rewatch note"},
    ]


def test_get_rewatch_notes_fills_blank_for_unwritten_rewatch(monkeypatch):
    """A rewatch that's happened (repeat_count within range) but has no note yet
    still gets a row in the list, pre-filled blank, so the form always shows an
    entry per rewatch that's occurred."""
    fake = FakeDB(rewatch_notes={(1, 100, 2): "only #2 has a note"})
    monkeypatch.setattr(main, "db", fake)

    result = main._get_rewatch_notes(user_id=1, anime_id=100, current_repeat_count=2)

    assert result == [
        {"repeat_count": 1, "note": ""},
        {"repeat_count": 2, "note": "only #2 has a note"},
    ]


def test_save_rewatch_note_rejects_repeat_count_beyond_current(monkeypatch):
    """Can't add a note for a rewatch that hasn't happened yet — repeat_count must
    be <= the user's actual library_entries.repeat_count for this anime."""
    fake = FakeDB(repeat_counts={(1, 100): 1})
    monkeypatch.setattr(main, "db", fake)

    applied = main._save_rewatch_note(user_id=1, anime_id=100, repeat_count=5, note="too soon")

    assert applied is False
    assert (1, 100, 5) not in fake.rewatch_notes


def test_save_rewatch_note_rejects_no_library_entry(monkeypatch):
    """No library_entries row at all for this user/anime -> rejected, not a crash."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)

    applied = main._save_rewatch_note(user_id=1, anime_id=100, repeat_count=1, note="anything")

    assert applied is False
    assert fake.rewatch_notes == {}


def test_blank_note_deletes_existing_rewatch_note(monkeypatch):
    """Saving a blank note for a rewatch that already had one clears it (lets a
    note be removed), rather than leaving a stale empty-string row."""
    fake = FakeDB(repeat_counts={(1, 100): 1}, rewatch_notes={(1, 100, 1): "will be cleared"})
    monkeypatch.setattr(main, "db", fake)

    applied = main._save_rewatch_note(user_id=1, anime_id=100, repeat_count=1, note="   ")

    assert applied is True
    assert (1, 100, 1) not in fake.rewatch_notes
