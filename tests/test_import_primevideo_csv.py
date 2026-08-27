"""
Coverage for issue #389 — Prime Video CSV import, the fallback path for when the
live cookie-header sync's session tier goes stale.

IMPORTANT: see scripts/import_primevideo_csv.py's module docstring — the real
export format was NOT confirmed against a live account during implementation, so
these tests exercise the parser's plausible-synonym column detection and its
fallback to Prime's own title-embedded season/episode heuristic (reused from
sync_primevideo.py), not a verified-real fixture. A real exported file must be
tested against this before treating the format as validated.

Also covers the two deliberate divergences from sync_netflix.py's own CSV
importer: this one never auto-creates a new AniList entry (only updates
already-tracked titles) and never touches primevideo_walk_complete — see the
module docstring for why.
"""

import csv
from datetime import datetime, timezone

import import_primevideo_csv as csvimp
import sync_primevideo as pv


# ── column detection ──────────────────────────────────────────────────────────

def test_find_column_matches_case_insensitively():
    assert csvimp._find_column(["Title", "Date"], csvimp._TITLE_COLUMN_SYNONYMS) == "Title"
    assert csvimp._find_column(["content title", "Date"], csvimp._TITLE_COLUMN_SYNONYMS) == "content title"


def test_find_column_returns_none_when_no_synonym_matches():
    assert csvimp._find_column(["Foo", "Bar"], csvimp._TITLE_COLUMN_SYNONYMS) is None


# ── date parsing ──────────────────────────────────────────────────────────────

def test_parse_date_mm_dd_yy():
    assert csvimp._parse_date("01/15/23") == datetime(2023, 1, 15, tzinfo=timezone.utc)


def test_parse_date_iso_with_time():
    assert csvimp._parse_date("2023-01-15T10:30:00") == datetime(2023, 1, 15, 10, 30, tzinfo=timezone.utc)


def test_parse_date_epoch_milliseconds():
    # Confirmed shape from sync_primevideo.py's own live API responses.
    ms = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert csvimp._parse_date(str(ms)) == datetime(2024, 6, 1, tzinfo=timezone.utc)


def test_parse_date_epoch_seconds():
    s = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp())
    assert csvimp._parse_date(str(s)) == datetime(2024, 6, 1, tzinfo=timezone.utc)


def test_parse_date_invalid_returns_none():
    assert csvimp._parse_date("not-a-date") is None


# ── parse_csv_rows ────────────────────────────────────────────────────────────

def _write_csv(tmp_path, rows, header):
    path = tmp_path / "primevideo.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return str(path)


def test_parse_csv_rows_with_explicit_season_episode_columns(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            ("MADE IN ABYSS", "01/10/24", "1", "5", "series"),
            ("Your Name.", "01/12/24", "", "", "movie"),
        ],
        header=("Title", "Date", "Season", "Episode", "Content Type"),
    )
    items, malformed = csvimp.parse_csv_rows(path)
    assert malformed == 0
    assert len(items) == 2
    ep = items[("made in abyss", 1)]
    assert ep["episode"] == 5
    assert ep["watched_format"] == "TV"
    movie = items[("your name.", 1)]
    assert movie["episode"] == 1
    assert movie["watched_format"] == "MOVIE"


def test_parse_csv_rows_falls_back_to_title_embedded_season(tmp_path):
    # No Season/Episode columns at all — must fall back to
    # sync_primevideo.py's own _parse_season_and_title()/_parse_episode_number()
    # heuristic, same as the live sync uses for Prime's inconsistent display text.
    path = _write_csv(
        tmp_path,
        [("MADE IN ABYSS - Season 1", "01/10/24")],
        header=("Title", "Date"),
    )
    items, malformed = csvimp.parse_csv_rows(path)
    assert malformed == 1  # no episode column and title has no "Episode N" text — nothing to derive episode from
    assert len(items) == 0


def test_parse_csv_rows_bare_season_title_skipped_not_false_matched(tmp_path):
    # Issue #387's exact incident shape ("Season 3" with no show name) — must be
    # skipped here too, not just in the live sync.
    path = _write_csv(
        tmp_path,
        [("Season 3", "01/10/24", "8", "")],
        header=("Title", "Date", "Episode", "Season"),
    )
    items, malformed = csvimp.parse_csv_rows(path)
    assert malformed == 1
    assert len(items) == 0


def test_parse_csv_rows_most_recent_watch_wins(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            ("MADE IN ABYSS", "01/05/24", "1", "3", "series"),
            ("MADE IN ABYSS", "01/10/24", "1", "1", "series"),  # rewatch started, later date, lower ep
        ],
        header=("Title", "Date", "Season", "Episode", "Content Type"),
    )
    items, malformed = csvimp.parse_csv_rows(path)
    assert malformed == 0
    assert items[("made in abyss", 1)]["episode"] == 1  # most recently watched, not highest


def test_parse_csv_rows_skips_malformed_rows_not_hard_failure(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            ("", "01/10/24", "1", "1", "series"),               # missing title
            ("Show A", "", "1", "1", "series"),                 # missing date
            ("Show B", "not-a-date", "1", "1", "series"),        # unparseable date
            ("Valid Movie", "01/12/24", "", "", "movie"),        # fine
        ],
        header=("Title", "Date", "Season", "Episode", "Content Type"),
    )
    items, malformed = csvimp.parse_csv_rows(path)
    assert malformed == 3
    assert len(items) == 1


def test_parse_csv_rows_raises_on_unrecognized_columns(tmp_path):
    path = _write_csv(tmp_path, [("x", "y")], header=("Foo", "Bar"))
    try:
        csvimp.parse_csv_rows(path)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Foo" in str(e) and "Bar" in str(e)


# ── run_import ────────────────────────────────────────────────────────────────

class _FakeConn:
    def close(self):
        pass


def _patch_common(monkeypatch, state=None, title_search_cache=None):
    monkeypatch.setattr(pv, "db_connect", lambda: _FakeConn())
    monkeypatch.setattr(pv, "ensure_table", lambda conn: None)
    monkeypatch.setattr(pv, "load_pv_state", lambda conn: state or {})
    monkeypatch.setattr(pv, "load_title_search_cache", lambda conn: title_search_cache or {})
    monkeypatch.setattr(pv, "save_title_search_cache_entry", lambda conn, title, mid: None)
    monkeypatch.setattr(pv, "save_watermark", lambda conn, media_id, title, watched_at: None)

    walk_complete_calls = []
    monkeypatch.setattr(
        pv, "set_walk_complete",
        lambda conn, provider, user_id, complete: walk_complete_calls.append(complete),
    )

    updates = []
    monkeypatch.setattr(pv, "_update", lambda conn, anilist_id, **kw: updates.append((anilist_id, kw)))
    monkeypatch.setattr(pv, "_save_state", lambda conn, anilist_id, title, last_ep, rewatch: None)

    monkeypatch.setattr(csvimp, "seed_search_cache", lambda cache: None)
    monkeypatch.setattr(csvimp, "is_plausible_match", lambda entry, fmt, ep: True)

    return updates, walk_complete_calls


def test_run_import_updates_already_tracked_title(tmp_path, monkeypatch):
    path = _write_csv(
        tmp_path,
        [("MADE IN ABYSS", "01/10/24", "1", "5", "series")],
        header=("Title", "Date", "Season", "Episode", "Content Type"),
    )
    updates, walk_complete_calls = _patch_common(monkeypatch)

    user_list = {97986: {"status": "CURRENT", "progress": 3, "repeat": 0,
                          "total_episodes": 13, "format": "TV", "title": "MADE IN ABYSS"}}
    title_index = {"made in abyss": 97986}
    monkeypatch.setattr(csvimp, "load_user_list_from_db", lambda: (user_list, title_index))
    monkeypatch.setattr(csvimp, "find_anilist_id", lambda title, index, season_number=1: index.get(title.lower()))

    summary = csvimp.run_import(path)

    assert summary["total_series"] == 1
    assert summary["updated"] == 1
    assert summary["skipped"] == 0
    assert updates == [(97986, {"progress": 5})]
    # Key divergence from sync_netflix.py's own CSV importer (issue #387's own
    # incident) — a bulk CSV import must never be the thing that flips a future
    # live sync into auto-create mode.
    assert walk_complete_calls == []


def test_run_import_never_creates_untracked_title(tmp_path, monkeypatch):
    path = _write_csv(
        tmp_path,
        [("Some Random Show", "01/10/24", "1", "1", "series")],
        header=("Title", "Date", "Season", "Episode", "Content Type"),
    )
    updates, walk_complete_calls = _patch_common(monkeypatch)

    # media_id resolves via search but is NOT in the user's tracked library —
    # must be skipped, never auto-created, unlike resolve_or_create_user_list_entry().
    monkeypatch.setattr(csvimp, "load_user_list_from_db", lambda: ({}, {}))
    monkeypatch.setattr(csvimp, "find_anilist_id", lambda title, index, season_number=1: 12345)

    summary = csvimp.run_import(path)

    assert summary["updated"] == 0
    assert summary["skipped"] == 1
    assert updates == []
    assert walk_complete_calls == []


def test_run_import_skips_unmatched_title(tmp_path, monkeypatch):
    path = _write_csv(
        tmp_path,
        [("Totally Unknown Thing", "01/10/24", "1", "1", "series")],
        header=("Title", "Date", "Season", "Episode", "Content Type"),
    )
    updates, walk_complete_calls = _patch_common(monkeypatch)

    monkeypatch.setattr(csvimp, "load_user_list_from_db", lambda: ({}, {}))
    monkeypatch.setattr(csvimp, "find_anilist_id", lambda title, index, season_number=1: None)

    summary = csvimp.run_import(path)

    assert summary["updated"] == 0
    assert summary["skipped"] == 1
    assert updates == []


def test_run_import_reports_malformed_row_count(tmp_path, monkeypatch):
    path = _write_csv(
        tmp_path,
        [
            ("MADE IN ABYSS", "01/10/24", "1", "5", "series"),
            ("", "01/10/24", "1", "1", "series"),
        ],
        header=("Title", "Date", "Season", "Episode", "Content Type"),
    )
    _patch_common(monkeypatch)
    user_list = {97986: {"status": "CURRENT", "progress": 3, "repeat": 0,
                          "total_episodes": 13, "format": "TV", "title": "MADE IN ABYSS"}}
    title_index = {"made in abyss": 97986}
    monkeypatch.setattr(csvimp, "load_user_list_from_db", lambda: (user_list, title_index))
    monkeypatch.setattr(csvimp, "find_anilist_id", lambda title, index, season_number=1: index.get(title.lower()))

    summary = csvimp.run_import(path)

    assert summary["malformed"] == 1


def test_run_import_respects_implausible_match_gate(tmp_path, monkeypatch):
    path = _write_csv(
        tmp_path,
        [("Wind River", "01/10/24", "", "", "movie")],
        header=("Title", "Date", "Season", "Episode", "Content Type"),
    )
    updates, _ = _patch_common(monkeypatch)

    # Already "tracked" locally (simulating a prior bad match already in the
    # library) but is_plausible_match rejects it — must still be skipped, not
    # blindly trusted just because it's in user_list.
    user_list = {10178: {"status": "CURRENT", "progress": 0, "repeat": 0,
                          "total_episodes": 4, "format": "SPECIAL", "title": "Otona Joshi no Anime Time"}}
    title_index = {"wind river": 10178}
    monkeypatch.setattr(csvimp, "load_user_list_from_db", lambda: (user_list, title_index))
    monkeypatch.setattr(csvimp, "find_anilist_id", lambda title, index, season_number=1: index.get(title.lower()))
    monkeypatch.setattr(csvimp, "is_plausible_match", lambda entry, fmt, ep: False)

    summary = csvimp.run_import(path)

    assert summary["updated"] == 0
    assert summary["skipped"] == 1
    assert updates == []
