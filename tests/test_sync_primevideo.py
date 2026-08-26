"""
Regression coverage for sync_primevideo.py (issue #17). process() is a
near-verbatim port of sync_plex.py's/sync_crunchyroll.py's process() — see
sync_crunchyroll.py's docstring for the full branch-by-branch reasoning
(including the #328 rewatch-clamp fix) — since Prime Video, like Crunchyroll
and Plex, reports real absolute episode numbers rather than Netflix's
delta-only counts. These tests mirror tests/test_sync_plex.py's process()
coverage 1:1 so the three can't silently drift, plus parse_items()/parsing-
helper coverage adapted to Prime Video's actual confirmed-live response shape
(see notes/2026-08-14-netflix-prime-sync-research.md's "Prime Video endpoint
— CONFIRMED" section) — nested date-sections of season/movie entries, each
season's episodes keyed by its own stable `gti`, episode numbers embedded in
a `"Episode N: <title>"` string rather than a dedicated field.
"""

from datetime import datetime, timezone

import sync_primevideo as pv

# ── _parse_episode_number() ──────────────────────────────────────────────────

def test_parse_episode_number_extracts_leading_number():
    assert pv._parse_episode_number("Episode 8: Pie") == 8
    assert pv._parse_episode_number("Episode 1: Welcome to Margrave") == 1


def test_parse_episode_number_case_insensitive():
    assert pv._parse_episode_number("episode 3: lowercase") == 3


def test_parse_episode_number_none_for_unparseable_text():
    assert pv._parse_episode_number("Special: Behind the Scenes") is None
    assert pv._parse_episode_number(None) is None
    assert pv._parse_episode_number("") is None


# ── _parse_season_and_title() ────────────────────────────────────────────────
# Real examples captured live 2026-08-26 (see the research notes) — season info
# is embedded inconsistently in the display title, not a separate field.

def test_parse_season_and_title_no_season_suffix_defaults_to_1():
    assert pv._parse_season_and_title("Reacher") == ("Reacher", 1)
    assert pv._parse_season_and_title("THE GHOST IN THE SHELL") == ("THE GHOST IN THE SHELL", 1)


def test_parse_season_and_title_dash_suffix():
    assert pv._parse_season_and_title("MADE IN ABYSS - Season 1") == ("MADE IN ABYSS", 1)
    assert pv._parse_season_and_title(
        "The Demon Sword Master of Excalibur Academy - Season 1"
    ) == ("The Demon Sword Master of Excalibur Academy", 1)


def test_parse_season_and_title_leading_zero_and_caps_variant():
    assert pv._parse_season_and_title("REACHER (TV) - SEASON 01") == ("REACHER (TV)", 1)


def test_parse_season_and_title_comma_suffix():
    assert pv._parse_season_and_title(
        "Georgie & Mandy's First Marriage, Season 2"
    ) == ("Georgie & Mandy's First Marriage", 2)


def test_parse_season_and_title_double_space_before_season():
    assert pv._parse_season_and_title("Lioness -  Season 2") == ("Lioness", 2)


def test_parse_season_and_title_bare_season_with_no_show_name_falls_back_to_original():
    # Observed live: a season entry with no show name attached at all — can't
    # extract a usable base title, so this deliberately keeps the original
    # string (which won't resolve against AniList, and gets skipped downstream
    # like any other unmatched title) rather than claiming a title of "".
    assert pv._parse_season_and_title("Season 3") == ("Season 3", 1)


def test_parse_season_and_title_empty_input():
    assert pv._parse_season_and_title(None) == ("", 1)
    assert pv._parse_season_and_title("") == ("", 1)


# ── parse_items() ─────────────────────────────────────────────────────────────

def _episode_item(gti="amzn1.dv.gti.season1", display_title="Reacher - Season 1",
                   episode_text="Episode 5: Foo", time_ms=1784000000000):
    return {
        "gti": gti, "display_title": display_title, "titleType": "episode",
        "episode_title_text": episode_text, "time": time_ms,
    }


def _movie_item(gti="amzn1.dv.gti.movie1", display_title="A Working Man",
                 time_ms=1784000000000):
    return {
        "gti": gti, "display_title": display_title, "titleType": "movie",
        "episode_title_text": None, "time": time_ms,
    }


def test_parse_items_picks_most_recently_watched_episode_not_highest():
    # A rewatch: ep 12 watched weeks ago, ep 1 watched yesterday — process()
    # needs the ep-1 position to detect the rewatch, not the historically-highest.
    older = _episode_item(episode_text="Episode 12: Old", time_ms=1723000000000)
    newer = _episode_item(episode_text="Episode 1: New", time_ms=1723600000000)
    result = pv.parse_items([older, newer])
    assert result["amzn1.dv.gti.season1"]["episode"] == 1


def test_parse_items_skips_items_with_unparseable_episode_text():
    special = _episode_item(episode_text="Special Feature")
    assert pv.parse_items([special]) == {}


def test_parse_items_skips_items_with_no_gti():
    no_gti = _episode_item(gti=None)
    assert pv.parse_items([no_gti]) == {}


def test_parse_items_keeps_two_seasons_of_same_franchise_separate_by_gti():
    season1 = _episode_item(gti="amzn1.dv.gti.s1", display_title="REACHER (TV) - SEASON 01",
                             episode_text="Episode 8: Pie", time_ms=1723000000000)
    season2 = _episode_item(gti="amzn1.dv.gti.s2", display_title="Reacher",
                             episode_text="Episode 3: Foo", time_ms=1723600000000)
    result = pv.parse_items([season1, season2])
    assert set(result.keys()) == {"amzn1.dv.gti.s1", "amzn1.dv.gti.s2"}
    assert result["amzn1.dv.gti.s1"]["episode"] == 8
    assert result["amzn1.dv.gti.s1"]["season"] == 1
    assert result["amzn1.dv.gti.s2"]["episode"] == 3


def test_parse_items_movie_gets_episode_1_and_movie_format():
    movie = _movie_item()
    result = pv.parse_items([movie])
    assert result["amzn1.dv.gti.movie1"]["episode"] == 1
    assert result["amzn1.dv.gti.movie1"]["watched_format"] == "MOVIE"
    assert result["amzn1.dv.gti.movie1"]["title"] == "A Working Man"


def test_parse_items_episode_gets_tv_format_and_parsed_season():
    ep = _episode_item(display_title="MADE IN ABYSS - Season 1", episode_text="Episode 4: Foo")
    result = pv.parse_items([ep])
    entry = result["amzn1.dv.gti.season1"]
    assert entry["watched_format"] == "TV"
    assert entry["title"] == "MADE IN ABYSS"
    assert entry["season"] == 1


# ── compute_fetch_watermark() ─────────────────────────────────────────────────

def test_compute_fetch_watermark_returns_max_across_series():
    state = {
        1: {"last_seen_watched_at": datetime(2026, 8, 1, tzinfo=timezone.utc)},
        2: {"last_seen_watched_at": datetime(2026, 8, 10, tzinfo=timezone.utc)},
    }
    assert pv.compute_fetch_watermark(state) == datetime(2026, 8, 10, tzinfo=timezone.utc)


def test_compute_fetch_watermark_none_when_no_state_recorded():
    assert pv.compute_fetch_watermark({}) is None
    assert pv.compute_fetch_watermark({1: {"last_seen_watched_at": None}}) is None


# ── process() — ported 1:1 from test_sync_plex.py ────────────────────────────

def _entry(status="CURRENT", progress=0, repeat=0, total=24, anilist_id=42):
    return {
        "status": status,
        "progress": progress,
        "repeat": repeat,
        "total_episodes": total,
        "anilist_id": anilist_id,
    }


def _capture(monkeypatch):
    calls = []
    monkeypatch.setattr(pv, "_update", lambda conn, anilist_id, **kw: calls.append(("update", anilist_id, kw)))
    monkeypatch.setattr(
        pv, "save_pv_state",
        lambda conn, anilist_id, title, last_ep, rewatch: calls.append(("save", anilist_id, last_ep, rewatch)),
    )
    return calls


def test_progress_advances_for_current_series(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="CURRENT", progress=3, total=24)
    result = pv.process("Attack on Titan", pv_ep=5, entry=entry, pv_state=None, conn=None)
    assert "progress 3 → 5" in result
    assert ("update", 42, {"progress": 5}) in calls


def test_dropped_series_resumes_to_current(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="DROPPED", progress=3, total=24)
    state = {"last_seen_episode": 3, "rewatch_in_progress": False}
    result = pv.process("Attack on Titan", pv_ep=5, entry=entry, pv_state=state, conn=None)
    assert "resumed" in result
    assert ("update", 42, {"progress": 5, "status": "CURRENT"}) in calls


def test_already_repeating_status_detected_and_advanced(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=2, repeat=1, total=24)
    result = pv.process("Attack on Titan", pv_ep=4, entry=entry, pv_state=None, conn=None)
    assert "rewatch detected" in result
    assert ("update", 42, {"progress": 4}) in calls


def test_completed_series_dropping_below_last_seen_starts_rewatch(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="COMPLETED", progress=24, repeat=0, total=24)
    state = {"last_seen_episode": 24, "rewatch_in_progress": False}
    result = pv.process("Attack on Titan", pv_ep=1, entry=entry, pv_state=state, conn=None)
    assert "rewatch started" in result
    assert ("update", 42, {"progress": 1, "status": "REPEATING"}) in calls


def test_rewatch_completion_increments_repeat(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=20, repeat=1, total=24)
    state = {"last_seen_episode": 20, "rewatch_in_progress": True}
    result = pv.process("Attack on Titan", pv_ep=24, entry=entry, pv_state=state, conn=None)
    assert "rewatch complete" in result
    assert ("update", 42, {"progress": 24, "status": "COMPLETED", "repeat": 2}) in calls


def test_first_sighting_of_completed_series_records_baseline_without_update(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="COMPLETED", progress=24, repeat=0, total=24)
    result = pv.process("Attack on Titan", pv_ep=24, entry=entry, pv_state=None, conn=None)
    assert "first-sync" in result
    assert not any(c[0] == "update" for c in calls)


def test_no_progress_since_last_sync_makes_no_anilist_call(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="CURRENT", progress=5, total=24)
    state = {"last_seen_episode": 5, "rewatch_in_progress": False}
    result = pv.process("Attack on Titan", pv_ep=5, entry=entry, pv_state=state, conn=None)
    assert "no change" in result
    assert not any(c[0] == "update" for c in calls)


# ── Same #328-shaped rewatch-clamp coverage as Crunchyroll/Plex ──────────────

def test_new_rewatch_pass_restarting_from_a_low_episode_is_detected(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=10, repeat=0, total=13)
    state = {"last_seen_episode": 10, "rewatch_in_progress": True}
    result = pv.process("Alderamin on the Sky", pv_ep=6, entry=entry, pv_state=state, conn=None)
    assert "new rewatch pass detected" in result
    assert ("update", 42, {"progress": 6}) in calls
    assert ("save", 42, 6, True) in calls


def test_pre_fix_behavior_would_have_silently_clamped_to_the_old_peak(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=10, repeat=0, total=13)
    state = {"last_seen_episode": 10, "rewatch_in_progress": True}
    pv.process("Alderamin on the Sky", pv_ep=6, entry=entry, pv_state=state, conn=None)
    save_calls = [c for c in calls if c[0] == "save"]
    assert len(save_calls) == 1
    assert save_calls[0][2] == 6, "must store the new pass's real episode, not the old high-water mark"


def test_equal_episode_while_rewatching_does_not_falsely_trigger_new_pass_detection(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status="REPEATING", progress=6, repeat=0, total=13)
    state = {"last_seen_episode": 6, "rewatch_in_progress": True}
    result = pv.process("Alderamin on the Sky", pv_ep=6, entry=entry, pv_state=state, conn=None)
    assert "new rewatch pass detected" not in result
    assert not any(c[0] == "update" for c in calls)


def test_new_entry_creates_watching_status_at_detected_episode(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status=None, progress=0, repeat=0, total=None)
    result = pv.process("The Testament of Sister New Devil", pv_ep=3, entry=entry, pv_state=None, conn=None)
    assert "new" in result.lower()
    assert ("update", 42, {"progress": 3, "status": "WATCHING"}) in calls
    assert ("save", 42, 3, False) in calls


def test_new_entry_branch_checked_before_every_other_branch(monkeypatch):
    calls = _capture(monkeypatch)
    entry = _entry(status=None, progress=0, repeat=0, total=12)
    result = pv.process("Some New Show", pv_ep=12, entry=entry, pv_state=None, conn=None)
    assert ("update", 42, {"progress": 12, "status": "WATCHING"}) in calls
    assert "COMPLETED" not in result
