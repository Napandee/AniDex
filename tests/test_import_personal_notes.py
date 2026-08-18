"""
Regression coverage for issue #87 — the /api/import restore path that reads
/api/export's JSON shape back in and upserts personal_notes for the requesting
user.

No real DB is touched: app.main's `db` module functions (fetchall/execute) are
monkeypatched against a tiny in-memory fake modeling just the two tables this
feature reads/writes (`anime`, `personal_notes`), matching the pattern used
elsewhere in this test suite (see test_run_full_sync.py) of monkeypatching the
handful of DB-touching calls rather than standing up a real Postgres.

Covers the three scenarios called out in #87: importing into a fresh account
(no confirmation needed), importing over existing notes (confirmation
required, then honored), and anilist_id-based matching against anime not
present locally (skipped, not a crash).
"""

import json

import app.main as main


class FakeDB:
    """Models just enough of `anime` and `personal_notes` for
    _analyze_personal_notes_import / _apply_personal_notes_import to run
    against, without a real Postgres connection."""

    def __init__(self, anime_ids, personal_notes=None):
        self.anime_ids = set(anime_ids)
        # {(user_id, anime_id): row dict}
        self.personal_notes = dict(personal_notes or {})
        self.execute_calls = []

    def fetchall(self, query, params=None):
        if "FROM anime" in query:
            ids = params[0]
            return [{"id": i} for i in ids if i in self.anime_ids]
        if "FROM personal_notes" in query:
            user_id, ids = params
            return [
                {"anime_id": aid}
                for (uid, aid) in self.personal_notes
                if uid == user_id and aid in ids
            ]
        raise AssertionError(f"unexpected query: {query}")

    def execute(self, query, params=None):
        self.execute_calls.append((query, params))
        assert "INSERT INTO personal_notes" in query
        (user_id, anime_id, drop_reason, tags_json, notes, priority, al_override) = params
        self.personal_notes[(user_id, anime_id)] = {
            "drop_reason": drop_reason,
            "personal_tags": json.loads(tags_json),
            "notes": notes,
            "watch_next_priority": priority,
            "anilist_id_override": al_override,
        }


def _entry(anilist_id, **overrides):
    e = {
        "anilist_id": anilist_id,
        "title_romaji": "Some Show",
        "status": "COMPLETED",
        "drop_reason": None,
        "notes": None,
        "personal_tags": [],
        "watch_next_priority": None,
        "anilist_id_override": None,
    }
    e.update(overrides)
    return e


def test_fresh_account_imports_without_confirmation(monkeypatch):
    """A brand-new account has no personal_notes at all — importing entries that
    do carry personal-layer data should write them straight away, no
    confirmation gate hit (overwrite_count stays 0)."""
    fake = FakeDB(anime_ids={100, 200})
    monkeypatch.setattr(main, "db", fake)

    entries = [
        _entry(100, notes="Great show", personal_tags=["comfort watch"]),
        _entry(200, drop_reason="Too slow"),
    ]

    analysis = main._analyze_personal_notes_import(user_id=1, entries=entries)

    assert analysis["overwrite_count"] == 0
    assert analysis["unmatched_count"] == 0
    assert len(analysis["importable"]) == 2
    assert not main._import_requires_confirmation(analysis, confirm=False)

    main._apply_personal_notes_import(user_id=1, importable=analysis["importable"])

    assert fake.personal_notes[(1, 100)]["notes"] == "Great show"
    assert fake.personal_notes[(1, 100)]["personal_tags"] == ["comfort watch"]
    assert fake.personal_notes[(1, 200)]["drop_reason"] == "Too slow"


def test_import_over_existing_notes_requires_confirmation(monkeypatch):
    """An account with an existing personal_notes row for a matched anime must
    not have it silently overwritten — the analysis should flag it, the gate
    should require confirm=True, and only after confirming should the write
    actually land."""
    fake = FakeDB(
        anime_ids={100},
        personal_notes={(1, 100): {"notes": "Old note I still care about"}},
    )
    monkeypatch.setattr(main, "db", fake)

    entries = [_entry(100, notes="Overwritten note from the import file")]

    analysis = main._analyze_personal_notes_import(user_id=1, entries=entries)
    assert analysis["overwrite_count"] == 1
    assert len(analysis["importable"]) == 1

    # Without confirm: the gate says stop, and nothing should have been written.
    assert main._import_requires_confirmation(analysis, confirm=False)
    assert fake.execute_calls == []

    # With confirm=True: the gate clears, apply proceeds.
    assert not main._import_requires_confirmation(analysis, confirm=True)
    main._apply_personal_notes_import(user_id=1, importable=analysis["importable"])
    assert fake.personal_notes[(1, 100)]["notes"] == "Overwritten note from the import file"


def test_unmatched_anilist_id_is_skipped_not_crashed(monkeypatch):
    """An anilist_id from the import file that has no local `anime` row (e.g.
    migrating personal-layer data to a fresh instance ahead of any sync run)
    must be skipped, never crash the import and never get written."""
    fake = FakeDB(anime_ids={100})
    monkeypatch.setattr(main, "db", fake)

    entries = [
        _entry(100, notes="Matches a local anime row"),
        _entry(999, notes="No local anime row for this one"),
    ]

    analysis = main._analyze_personal_notes_import(user_id=1, entries=entries)

    assert analysis["unmatched_count"] == 1
    assert [row[0] for row in analysis["importable"]] == [100]

    # Applying should not raise, and should only ever touch the matched row.
    main._apply_personal_notes_import(user_id=1, importable=analysis["importable"])
    assert (1, 999) not in fake.personal_notes
    assert fake.personal_notes[(1, 100)]["notes"] == "Matches a local anime row"


def test_entries_with_no_personal_data_are_never_touched(monkeypatch):
    """The bulk of a real /api/export dump is library entries with every
    personal_notes field null (nothing was ever set). Those must not generate
    writes, count toward the overwrite gate, or otherwise be "mentioned" by the
    import at all."""
    fake = FakeDB(anime_ids={100, 200})
    monkeypatch.setattr(main, "db", fake)

    entries = [_entry(100), _entry(200)]  # all personal fields left at defaults (None/[])

    analysis = main._analyze_personal_notes_import(user_id=1, entries=entries)

    assert analysis["importable"] == []
    assert analysis["overwrite_count"] == 0
    assert analysis["unmatched_count"] == 0

    main._apply_personal_notes_import(user_id=1, importable=analysis["importable"])
    assert fake.execute_calls == []
