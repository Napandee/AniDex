"""
Coverage for issue #300 — the notes-page "Filler episodes" section, sourced
from #299's three-table cache (filler_episode_cache / filler_sync_state /
filler_data_license, see schema.sql).

No real DB is touched: app.main's `db` module functions (fetchone/fetchall)
are monkeypatched against a tiny in-memory fake, matching the pattern used
elsewhere in this suite (see test_rewatch_notes.py). Tests call the route
helpers (_group_consecutive_episodes / _get_filler_data / _get_filler_license)
directly rather than through FastAPI's TestClient, since notes_form itself is
a thin wrapper around these that just adds session-auth plumbing and the rest
of the page's unrelated context (trailer/related/also-watching/rewatch notes).

Covers the three-way "not yet checked" / "checked, no match" / "checked,
matched" distinction #300's acceptance criteria call out, plus the
consecutive-episode-range grouping and citation representative-selection
logic that back the template's expandable filler/mixed lists.
"""

import app.main as main


class FakeDB:
    """Models just enough of filler_sync_state / filler_episode_cache /
    filler_data_license for _get_filler_data / _get_filler_license to run
    against, without a real Postgres connection."""

    def __init__(self, sync_state=None, episode_rows=None, license_row=None):
        # {anime_id: {"afp_series_id": int|None, "last_checked_at": ...}}
        self.sync_state = dict(sync_state or {})
        # {anime_id: [row dicts]}
        self.episode_rows = dict(episode_rows or {})
        self.license_row = license_row

    def fetchone(self, query, params=None):
        if "FROM filler_sync_state" in query:
            (anime_id,) = params
            return self.sync_state.get(anime_id)
        if "FROM filler_data_license" in query:
            return self.license_row
        raise AssertionError(f"unexpected query: {query}")

    def fetchall(self, query, params=None):
        if "FROM filler_episode_cache" in query:
            (anime_id,) = params
            rows = self.episode_rows.get(anime_id, [])
            return sorted(rows, key=lambda r: r["episode_number"])
        raise AssertionError(f"unexpected query: {query}")


def _ep(number, status, citation_url=None, citation_description=None, status_note=None):
    return {
        "episode_number": number,
        "status": status,
        "status_note": status_note,
        "citation_url": citation_url,
        "citation_description": citation_description,
    }


# ── _group_consecutive_episodes ──────────────────────────────────────────

def test_group_consecutive_episodes_collapses_a_run_into_one_range():
    episodes = [_ep(12, "filler"), _ep(13, "filler"), _ep(14, "filler"), _ep(15, "filler")]

    groups = main._group_consecutive_episodes(episodes)

    assert len(groups) == 1
    assert groups[0]["label"] == "12-15"
    assert groups[0]["start"] == 12 and groups[0]["end"] == 15


def test_group_consecutive_episodes_keeps_a_gap_as_two_groups():
    episodes = [_ep(12, "filler"), _ep(13, "filler"), _ep(20, "filler")]

    groups = main._group_consecutive_episodes(episodes)

    assert [g["label"] for g in groups] == ["12-13", "20"]


def test_group_consecutive_episodes_single_episode_is_not_a_range():
    groups = main._group_consecutive_episodes([_ep(5, "mixed")])

    assert groups[0]["label"] == "5"


def test_group_consecutive_episodes_picks_first_episode_with_a_citation_as_representative():
    episodes = [
        _ep(1, "filler", citation_url=None, citation_description=None),
        _ep(2, "filler", citation_url="https://afp.example/2", citation_description="cut for pacing"),
        _ep(3, "filler", citation_url="https://afp.example/3", citation_description="different source"),
    ]

    groups = main._group_consecutive_episodes(episodes)

    assert len(groups) == 1
    assert groups[0]["label"] == "1-3"
    assert groups[0]["citation_url"] == "https://afp.example/2"
    assert groups[0]["citation_description"] == "cut for pacing"


def test_group_consecutive_episodes_falls_back_to_first_episode_when_none_cited():
    episodes = [_ep(7, "canon"), _ep(8, "canon")]

    groups = main._group_consecutive_episodes(episodes)

    assert groups[0]["citation_url"] is None
    assert groups[0]["citation_description"] is None


# ── _get_filler_data: the three-way "not checked" / "no match" / "matched" split ──

def test_never_checked_returns_none_so_caller_renders_no_section_at_all(monkeypatch):
    """No filler_sync_state row at all -> the sync job has never attempted this
    title. #300 requires no section at all here, not even an empty state."""
    fake = FakeDB()
    monkeypatch.setattr(main, "db", fake)

    result = main._get_filler_data(anime_id=999)

    assert result is None


def test_checked_no_match_returns_no_match_state(monkeypatch):
    """A filler_sync_state row exists but afp_series_id is NULL -> AniFillerPedia
    has no series matching this title (yet). Distinct from "never checked"."""
    fake = FakeDB(sync_state={100: {"afp_series_id": None, "last_checked_at": "2026-08-20"}})
    monkeypatch.setattr(main, "db", fake)

    result = main._get_filler_data(anime_id=100)

    assert result["state"] == "no_match"
    assert result["last_checked_at"] == "2026-08-20"


def test_checked_matched_but_zero_episodes_returns_matched_empty_state(monkeypatch):
    """afp_series_id is set (a real match) but filler_episode_cache has no rows
    for it yet -> matched, but nothing researched on AniFillerPedia's side."""
    fake = FakeDB(sync_state={100: {"afp_series_id": 42, "last_checked_at": "2026-08-20"}})
    monkeypatch.setattr(main, "db", fake)

    result = main._get_filler_data(anime_id=100)

    assert result["state"] == "matched_empty"


def test_checked_matched_with_episodes_returns_counts_and_groups(monkeypatch):
    rows = {
        100: [
            _ep(1, "canon"),
            _ep(2, "canon"),
            _ep(3, "filler", citation_url="https://afp.example/3", citation_description="filler arc"),
            _ep(4, "filler", citation_url="https://afp.example/3", citation_description="filler arc"),
            _ep(10, "mixed", status_note="partially adapted"),
        ]
    }
    fake = FakeDB(sync_state={100: {"afp_series_id": 42, "last_checked_at": "2026-08-20"}}, episode_rows=rows)
    monkeypatch.setattr(main, "db", fake)

    result = main._get_filler_data(anime_id=100)

    assert result["state"] == "matched"
    assert result["counts"] == {"canon": 2, "filler": 2, "mixed": 1}
    assert result["total_researched"] == 5
    assert [g["label"] for g in result["groups"]["canon"]] == ["1-2"]
    assert [g["label"] for g in result["groups"]["filler"]] == ["3-4"]
    assert [g["label"] for g in result["groups"]["mixed"]] == ["10"]
    assert result["groups"]["filler"][0]["citation_url"] == "https://afp.example/3"


# ── _get_filler_license ──────────────────────────────────────────────────

def test_get_filler_license_returns_none_when_no_row(monkeypatch):
    fake = FakeDB(license_row=None)
    monkeypatch.setattr(main, "db", fake)

    assert main._get_filler_license() is None


def test_get_filler_license_returns_attribution_dict(monkeypatch):
    fake = FakeDB(license_row={"license_name": "CC BY-NC-SA 4.0", "attribution_notice": "Data from AniFillerPedia."})
    monkeypatch.setattr(main, "db", fake)

    result = main._get_filler_license()

    assert result == {"license_name": "CC BY-NC-SA 4.0", "attribution_notice": "Data from AniFillerPedia."}
