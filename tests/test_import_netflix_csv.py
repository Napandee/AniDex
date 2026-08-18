import csv
from datetime import datetime, timezone

import pytest

import import_netflix_csv as csvimp
import sync_netflix as nf


# ── extract_series_title heuristic (issue #98) ───────────────────────────────

def test_season_marker_always_splits():
    title, is_ep = csvimp.extract_series_title("Attack on Titan: Season 3: Part 2", {})
    assert title == "Attack on Titan"
    assert is_ep is True


def test_season_marker_case_insensitive_no_trailing_segment():
    title, is_ep = csvimp.extract_series_title("Frieren: season 1", {})
    assert title == "Frieren"
    assert is_ep is True


def test_single_colon_splits_only_when_prefix_is_known_library_title():
    index = {"death note": 1}
    title, is_ep = csvimp.extract_series_title("Death Note: Episode 5", index)
    assert title == "Death Note"
    assert is_ep is True


def test_single_colon_does_not_split_when_prefix_unknown():
    # The exact counter-example the issue itself calls out — a colon inside a real
    # movie title must not get treated as a TV hierarchy separator.
    title, is_ep = csvimp.extract_series_title("Mission: Impossible - Dead Reckoning", {})
    assert title == "Mission: Impossible - Dead Reckoning"
    assert is_ep is False


def test_bare_title_no_colon_treated_as_movie():
    title, is_ep = csvimp.extract_series_title("Your Name.", {})
    assert title == "Your Name."
    assert is_ep is False


def test_movie_title_containing_colon_matches_whole_title_when_in_library():
    # A real anime movie whose own title contains a colon, already in the library —
    # the prefix-based split still shouldn't fire since there's no "Season" marker
    # and the raw title itself (not just its prefix) is what's in the index.
    index = {"fullmetal alchemist: the sacred star of milos": 5}
    title, is_ep = csvimp.extract_series_title("Fullmetal Alchemist: The Sacred Star of Milos", index)
    assert title == "Fullmetal Alchemist: The Sacred Star of Milos"
    assert is_ep is False


# ── date parsing ──────────────────────────────────────────────────────────────

def test_parse_date_mm_dd_yy():
    assert csvimp._parse_date("01/15/23") == datetime(2023, 1, 15, tzinfo=timezone.utc)


def test_parse_date_iso():
    assert csvimp._parse_date("2023-01-15") == datetime(2023, 1, 15, tzinfo=timezone.utc)


def test_parse_date_invalid_returns_none():
    assert csvimp._parse_date("not-a-date") is None


# ── parse_csv_rows ────────────────────────────────────────────────────────────

def _write_csv(tmp_path, rows, header=("Title", "Date")):
    path = tmp_path / "netflix.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return str(path)


def test_parse_csv_rows_happy_path(tmp_path):
    path = _write_csv(tmp_path, [
        ("Frieren: Beyond Journey's End: Season 1: Episode 1", "01/10/24"),
        ("Your Name.", "01/12/24"),
    ])
    items, malformed = csvimp.parse_csv_rows(path, {})
    assert malformed == 0
    assert len(items) == 2
    ep = next(i for i in items if i["seriesTitle"].startswith("Frieren"))
    assert ep["series"] == 1
    movie = next(i for i in items if i["seriesTitle"] == "Your Name.")
    assert movie["series"] is None


def test_parse_csv_rows_skips_malformed_rows_not_hard_failure(tmp_path):
    path = _write_csv(tmp_path, [
        ("", "01/10/24"),                                  # missing title
        ("Some Show: Season 1: Episode 1", ""),             # missing date
        ("Some Show: Season 1: Episode 2", "not-a-date"),   # unparseable date
        ("Valid Movie", "01/12/24"),                        # fine
    ])
    items, malformed = csvimp.parse_csv_rows(path, {})
    assert malformed == 3
    assert len(items) == 1


def test_parse_csv_rows_raises_on_missing_columns(tmp_path):
    path = _write_csv(tmp_path, [("x", "y")], header=("Movie", "WatchedOn"))
    with pytest.raises(ValueError):
        csvimp.parse_csv_rows(path, {})


# ── run_import: per-series bounded-diff pipeline ─────────────────────────────

class _FakeConn:
    def close(self):
        pass


def _patch_common(monkeypatch, nf_state=None, title_search_cache=None):
    monkeypatch.setattr(nf, "db_connect", lambda: _FakeConn())
    monkeypatch.setattr(nf, "ensure_table", lambda conn: None)
    monkeypatch.setattr(nf, "load_nf_state", lambda conn: nf_state or {})
    monkeypatch.setattr(nf, "load_title_search_cache", lambda conn: title_search_cache or {})
    monkeypatch.setattr(nf, "save_title_search_cache_entry", lambda conn, title, mid: None)
    walk_complete_calls = []
    monkeypatch.setattr(nf, "_set_walk_complete", lambda conn, complete: walk_complete_calls.append(complete))

    updates = []
    monkeypatch.setattr(nf, "_update", lambda anilist_id, **kw: updates.append((anilist_id, kw)))
    monkeypatch.setattr(nf, "_save_state", lambda conn, anilist_id, title, watched_at, rewatch: None)

    monkeypatch.setattr(csvimp, "seed_search_cache", lambda cache: None)
    monkeypatch.setattr(csvimp, "is_plausible_match", lambda entry, fmt, ep: True)

    return updates, walk_complete_calls


def test_run_import_matches_and_updates_progress(tmp_path, monkeypatch):
    path = _write_csv(tmp_path, [
        ("Frieren: Beyond Journey's End: Season 1: Episode 2", "01/10/24"),
        ("Frieren: Beyond Journey's End: Season 1: Episode 1", "01/09/24"),
    ])
    updates, walk_complete_calls = _patch_common(monkeypatch)

    user_list = {170068: {"status": "CURRENT", "progress": 0, "repeat": 0,
                           "total_episodes": 28, "format": "TV",
                           "title": "Frieren: Beyond Journey's End"}}
    title_index = {"frieren: beyond journey's end": 170068}
    monkeypatch.setattr(csvimp, "load_user_list_from_db", lambda: (user_list, title_index))
    monkeypatch.setattr(csvimp, "find_anilist_id", lambda title, index: index.get(title.lower()))

    summary = csvimp.run_import(path)

    assert summary["total_series"] == 1
    assert summary["updated"] == 1
    assert summary["skipped"] == 0
    assert walk_complete_calls == [True]
    assert updates == [(170068, {"progress": 2})]


def test_run_import_skips_series_not_in_library(tmp_path, monkeypatch):
    path = _write_csv(tmp_path, [("Some Random Show: Season 1: Episode 1", "01/10/24")])
    updates, walk_complete_calls = _patch_common(monkeypatch)

    monkeypatch.setattr(csvimp, "load_user_list_from_db", lambda: ({}, {}))
    monkeypatch.setattr(csvimp, "find_anilist_id", lambda title, index: None)

    summary = csvimp.run_import(path)

    assert summary["updated"] == 0
    assert summary["skipped"] == 1
    assert updates == []
    # Import still counts as a full, authoritative review of the export even when
    # nothing in it matched the current library.
    assert walk_complete_calls == [True]


def test_run_import_reports_malformed_row_count(tmp_path, monkeypatch):
    path = _write_csv(tmp_path, [
        ("Frieren: Beyond Journey's End: Season 1: Episode 1", "01/09/24"),
        ("", "01/10/24"),
    ])
    _patch_common(monkeypatch)
    user_list = {170068: {"status": "CURRENT", "progress": 0, "repeat": 0,
                           "total_episodes": 28, "format": "TV",
                           "title": "Frieren: Beyond Journey's End"}}
    title_index = {"frieren: beyond journey's end": 170068}
    monkeypatch.setattr(csvimp, "load_user_list_from_db", lambda: (user_list, title_index))
    monkeypatch.setattr(csvimp, "find_anilist_id", lambda title, index: index.get(title.lower()))

    summary = csvimp.run_import(path)

    assert summary["malformed"] == 1
