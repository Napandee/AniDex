"""
Coverage for issue #210 — attaching a note to a specific episode, surfaced next
to the progress stepper, with a gentle post-increment auto-suggest prompt.

No real DB is touched: app.main's `db` module functions (fetchall/execute) are
monkeypatched against a tiny in-memory fake modeling `library_entries` and
`episode_notes`, matching the pattern used by test_rewatch_notes.py (this
table is deliberately the same shape as rewatch_notes, just keyed on
episode_number/progress instead of repeat_count/repeat_count). Tests call the
route helpers (_get_episode_note / _save_episode_note) directly rather than
through FastAPI's TestClient, since the routes themselves are thin wrappers
around these that just add session-auth + JSON plumbing.

Also covers, at the unit level, that the client-side auto-suggest logic (see
app/static/script.js's suggestEpisodeNote / progress-stepper save()) is wired
so a plain progress increment can never be blocked or delayed by it: the save()
function only calls suggestEpisodeNote() *after* its own fetch to
/api/anime/{id}/progress has already resolved successfully, and does so
without awaiting the result — verified here by asserting that shape directly
in the source (an integration/browser check would be the alternative, but this
catches a regression here — someone accidentally awaiting the call, or moving
it before the progress fetch — without needing a live server).
"""

import re
from pathlib import Path

import app.main as main


class FakeDB:
    """Models just enough of `library_entries` and `episode_notes` for
    _get_episode_note / _save_episode_note to run against, without a real
    Postgres connection."""

    def __init__(self, progress=None, episode_notes=None):
        # {(user_id, anime_id): progress}
        self.progress = dict(progress or {})
        # {(user_id, anime_id, episode_number): note}
        self.episode_notes = dict(episode_notes or {})
        self.execute_calls = []

    def fetchone(self, query, params=None):
        if "FROM library_entries" in query:
            anime_id, user_id = params
            p = self.progress.get((user_id, anime_id))
            return {"progress": p} if p is not None else None
        if "FROM episode_notes" in query:
            user_id, anime_id, episode_number = params
            note = self.episode_notes.get((user_id, anime_id, episode_number))
            return {"note": note} if note is not None else None
        raise AssertionError(f"unexpected query: {query}")

    def fetchall(self, query, params=None):
        raise AssertionError(f"unexpected query: {query}")

    def execute(self, query, params=None):
        self.execute_calls.append((query, params))
        if "INSERT INTO episode_notes" in query:
            user_id, anime_id, episode_number, note = params
            self.episode_notes[(user_id, anime_id, episode_number)] = note
        elif "DELETE FROM episode_notes" in query:
            user_id, anime_id, episode_number = params
            self.episode_notes.pop((user_id, anime_id, episode_number), None)
        else:
            raise AssertionError(f"unexpected query: {query}")


def test_get_episode_note_returns_empty_string_when_none_exists(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)

    assert main._get_episode_note(user_id=1, anime_id=100, episode_number=3) == ""


def test_save_and_get_episode_note_roundtrip(monkeypatch):
    fake = FakeDB(progress={(1, 100): 5})
    monkeypatch.setattr(main, "db", fake)

    applied = main._save_episode_note(user_id=1, anime_id=100, episode_number=3, note="great fight scene")

    assert applied is True
    assert main._get_episode_note(user_id=1, anime_id=100, episode_number=3) == "great fight scene"
    # Only episode_notes queries were issued — no personal_notes/rewatch_notes touch.
    for query, _ in fake.execute_calls:
        assert "personal_notes" not in query
        assert "rewatch_notes" not in query


def test_save_episode_note_rejects_episode_beyond_current_progress(monkeypatch):
    """Can't add a note for an episode that hasn't been watched yet —
    episode_number must be <= the user's actual library_entries.progress."""
    fake = FakeDB(progress={(1, 100): 3})
    monkeypatch.setattr(main, "db", fake)

    applied = main._save_episode_note(user_id=1, anime_id=100, episode_number=5, note="too soon")

    assert applied is False
    assert (1, 100, 5) not in fake.episode_notes


def test_save_episode_note_rejects_no_library_entry(monkeypatch):
    """No library_entries row at all for this user/anime -> rejected, not a crash."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)

    applied = main._save_episode_note(user_id=1, anime_id=100, episode_number=1, note="anything")

    assert applied is False
    assert fake.episode_notes == {}


def test_save_episode_note_rejects_episode_zero(monkeypatch):
    fake = FakeDB(progress={(1, 100): 5})
    monkeypatch.setattr(main, "db", fake)

    applied = main._save_episode_note(user_id=1, anime_id=100, episode_number=0, note="nope")

    assert applied is False


def test_blank_note_deletes_existing_episode_note(monkeypatch):
    """Saving a blank note for an episode that already had one clears it (lets a
    note be removed), rather than leaving a stale empty-string row — same
    convention as _save_rewatch_note."""
    fake = FakeDB(progress={(1, 100): 3}, episode_notes={(1, 100, 3): "will be cleared"})
    monkeypatch.setattr(main, "db", fake)

    applied = main._save_episode_note(user_id=1, anime_id=100, episode_number=3, note="   ")

    assert applied is True
    assert (1, 100, 3) not in fake.episode_notes


def test_multiple_episode_notes_stored_independently(monkeypatch):
    """Notes for episode 1 and episode 2 of the same anime are independent —
    saving one doesn't clobber the other."""
    fake = FakeDB(progress={(1, 100): 4})
    monkeypatch.setattr(main, "db", fake)

    assert main._save_episode_note(user_id=1, anime_id=100, episode_number=1, note="cold open note")
    assert main._save_episode_note(user_id=1, anime_id=100, episode_number=2, note="ed change note")

    assert main._get_episode_note(user_id=1, anime_id=100, episode_number=1) == "cold open note"
    assert main._get_episode_note(user_id=1, anime_id=100, episode_number=2) == "ed change note"


def test_editing_an_existing_episode_note_overwrites_it(monkeypatch):
    fake = FakeDB(progress={(1, 100): 3}, episode_notes={(1, 100, 3): "first draft"})
    monkeypatch.setattr(main, "db", fake)

    applied = main._save_episode_note(user_id=1, anime_id=100, episode_number=3, note="edited note")

    assert applied is True
    assert main._get_episode_note(user_id=1, anime_id=100, episode_number=3) == "edited note"


def test_unique_constraint_per_user_anime_episode_in_schema():
    """The migration/schema must declare UNIQUE (user_id, anime_id, episode_number),
    matching rewatch_notes' UNIQUE (user_id, anime_id, repeat_count) shape exactly —
    this is what the app's ON CONFLICT upsert in _save_episode_note relies on."""
    repo_root = Path(__file__).resolve().parent.parent
    schema_text = (repo_root / "schema.sql").read_text(encoding="utf-8")
    migration_text = (repo_root / "migrations" / "019_episode_notes.sql").read_text(encoding="utf-8")

    for text, label in [(schema_text, "schema.sql"), (migration_text, "migrations/019_episode_notes.sql")]:
        assert re.search(r"CREATE TABLE episode_notes", text), f"episode_notes table missing from {label}"
        assert re.search(r"UNIQUE\s*\(user_id,\s*anime_id,\s*episode_number\)", text), (
            f"episode_notes missing UNIQUE (user_id, anime_id, episode_number) in {label}"
        )
        assert re.search(r"episode_number\s+INTEGER NOT NULL CHECK\s*\(episode_number >= 1\)", text), (
            f"episode_notes.episode_number missing CHECK (episode_number >= 1) in {label}"
        )


def test_progress_stepper_save_does_not_await_the_suggestion_call():
    """The whole point of the auto-suggest prompt is that it can never slow down
    or block a plain progress increment. Guard that shape directly in the JS
    source: suggestEpisodeNote(...) must be called without `await` inside
    save(), and only after the progress fetch's own `resp.ok` check — so a
    user who ignores/dismisses the prompt (or whose browser is offline for the
    suggestion lookup) still gets an ordinary, uninterrupted progress update."""
    repo_root = Path(__file__).resolve().parent.parent
    script_js = (repo_root / "app" / "static" / "script.js").read_text(encoding="utf-8")

    save_fn_match = re.search(r"async function save\(\)\s*\{(.*?)\n  \}", script_js, re.DOTALL)
    assert save_fn_match, "could not locate progress-stepper's save() function in script.js"
    save_body = save_fn_match.group(1)

    assert "suggestEpisodeNote(animeId, target)" in save_body
    assert "await suggestEpisodeNote" not in save_body
    # The suggestion call must come after the progress fetch's success check,
    # not before it — a naive refactor could otherwise fire it speculatively
    # ahead of (or regardless of) the actual save succeeding.
    assert save_body.index("resp.ok") < save_body.index("suggestEpisodeNote(animeId, target)")
