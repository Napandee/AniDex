"""
Coverage for issue #224 — anime-native stats on /stats: an episode-count vs.
movie-runtime watch-time split (_compute_format_watch_time), and a seasonal
follow-through rate (_compute_seasonal_follow_through) for shows started
during their original airing window.

No real DB is touched: app.main's `db` module is monkeypatched against small
in-memory FakeDB classes modeling just enough of the queries each function
issues, matching the pattern used elsewhere in this suite (test_rewatch_stats.py,
test_taste_drift.py) — real Postgres isn't stood up for unit tests.

_compute_format_watch_time coverage:
- episode count only sums non-MOVIE-format progress; movie minutes only sums
  MOVIE-format progress * duration
- a movie with no recorded duration falls back to MOVIE_DEFAULT_DURATION_MINUTES,
  not the 24-minute TV-episode default used by the blended totals.watch_minutes
  stat elsewhere on this same endpoint
- a movie that's only Planned (progress == 0) doesn't count toward movie_count
  or movie_minutes
- a brand-new account (no rows at all) returns all-zero, not an error

_compute_seasonal_follow_through coverage:
- an entry started well within the season's airing window (including the
  pre-buffer before the season's nominal start month) counts
- an entry started long after the season aired (a later binge) is excluded
  entirely — the exact scenario issue #224's "out of scope" note calls out
- COMPLETED/REPEATING count as followed-through; DROPPED counts as
  dropped-or-stalled; WATCHING/PAUSED against a FINISHED anime also counts as
  dropped-or-stalled (never caught up after the show ended)
- WATCHING/PAUSED against a still-RELEASING anime is excluded from both the
  numerator and denominator (no verdict possible yet) but tracked separately
  as excluded_still_airing
- PLANNING entries never count, even if start_date happens to be set
- MOVIE-format entries are excluded regardless of season/season_year
- returns None below SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES *judged* entries,
  even if there are plenty of still-airing-excluded ones padding the total
- per-user scoping
"""

from datetime import date

import app.main as main


class FormatSplitFakeDB:
    """Models the single aggregate query _compute_format_watch_time issues.
    entries: list of dicts {user_id, format, progress, duration}."""

    def __init__(self, entries=None):
        self.entries = entries or []

    def fetchone(self, query, params=None):
        default_duration, user_id = params
        mine = [e for e in self.entries if e["user_id"] == user_id]
        episode_count = sum(e["progress"] for e in mine if e["format"] != "MOVIE")
        movies_watched = [e for e in mine if e["format"] == "MOVIE" and e["progress"] > 0]
        movie_count = len(movies_watched)
        movie_minutes = sum(
            e["progress"] * (e["duration"] if e["duration"] is not None else default_duration)
            for e in movies_watched
        )
        return {
            "episode_count": episode_count,
            "movie_count": movie_count,
            "movie_minutes": movie_minutes,
        }


def _fs_entry(user_id=1, format="TV", progress=0, duration=24):
    return {"user_id": user_id, "format": format, "progress": progress, "duration": duration}


def test_format_split_brand_new_account_is_all_zero(monkeypatch):
    monkeypatch.setattr(main, "db", FormatSplitFakeDB([]))
    result = main._compute_format_watch_time(user_id=1)
    assert result == {
        "episode_count": 0,
        "movie_count": 0,
        "movie_minutes": 0,
        "movie_hours": 0.0,
    }


def test_format_split_separates_episodes_from_movie_minutes(monkeypatch):
    fake = FormatSplitFakeDB([
        _fs_entry(format="TV", progress=24, duration=23),
        _fs_entry(format="OVA", progress=2, duration=30),
        _fs_entry(format="MOVIE", progress=1, duration=105),
        _fs_entry(format="MOVIE", progress=1, duration=95),
    ])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_format_watch_time(user_id=1)

    assert result["episode_count"] == 26  # 24 TV + 2 OVA, never converted to minutes
    assert result["movie_count"] == 2
    assert result["movie_minutes"] == 200
    assert result["movie_hours"] == round(200 / 60, 1)


def test_format_split_movie_with_no_duration_uses_movie_default_not_tv_default(monkeypatch):
    fake = FormatSplitFakeDB([_fs_entry(format="MOVIE", progress=1, duration=None)])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_format_watch_time(user_id=1)

    assert result["movie_minutes"] == main.MOVIE_DEFAULT_DURATION_MINUTES


def test_format_split_planned_only_movie_does_not_count(monkeypatch):
    """progress == 0 (Planning, never actually watched) must not inflate
    movie_count or movie_minutes."""
    fake = FormatSplitFakeDB([_fs_entry(format="MOVIE", progress=0, duration=100)])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_format_watch_time(user_id=1)

    assert result["movie_count"] == 0
    assert result["movie_minutes"] == 0


class SeasonalFakeDB:
    """Models the single query _compute_seasonal_follow_through issues.
    entries: list of dicts {user_id, entry_status, start_date, anime_status,
    season, season_year, format}. The real WHERE clause filters out PLANNING,
    NULL start_date, missing season/season_year, and MOVIE format — replicated
    here so tests exercise the same filtering the real query would do."""

    def __init__(self, entries=None):
        self.entries = entries or []

    def fetchall(self, query, params=None):
        (user_id,) = params
        return [
            {
                "entry_status": e["entry_status"],
                "start_date": e["start_date"],
                "anime_status": e["anime_status"],
                "season": e["season"],
                "season_year": e["season_year"],
            }
            for e in self.entries
            if e["user_id"] == user_id
            and e["entry_status"] != "PLANNING"
            and e["start_date"] is not None
            and e["season"] is not None
            and e["season_year"] is not None
            and e.get("format", "TV") != "MOVIE"
        ]


def _sf_entry(
    user_id=1,
    entry_status="COMPLETED",
    start_date=date(2026, 1, 5),
    anime_status="FINISHED",
    season="WINTER",
    season_year=2026,
    format="TV",
):
    return {
        "user_id": user_id,
        "entry_status": entry_status,
        "start_date": start_date,
        "anime_status": anime_status,
        "season": season,
        "season_year": season_year,
        "format": format,
    }


def _fill(n, **kwargs):
    """Helper to pad the judged sample above SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES
    with straightforward followed-through entries."""
    return [_sf_entry(**kwargs) for _ in range(n)]


def test_returns_none_below_min_samples(monkeypatch):
    fake = SeasonalFakeDB(_fill(main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES - 1))
    monkeypatch.setattr(main, "db", fake)
    assert main._compute_seasonal_follow_through(user_id=1) is None


def test_still_airing_excluded_entries_do_not_count_toward_min_samples(monkeypatch):
    """A pile of still-WATCHING-against-RELEASING entries must not push the
    section past the sample gate on their own — only judged (followed or
    dropped/stalled) entries count toward SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES."""
    entries = _fill(20, entry_status="WATCHING", anime_status="RELEASING")
    fake = SeasonalFakeDB(entries)
    monkeypatch.setattr(main, "db", fake)
    assert main._compute_seasonal_follow_through(user_id=1) is None


def test_binge_long_after_airing_window_is_excluded(monkeypatch):
    """Issue #224's explicit out-of-scope scenario: a show Planned then binged
    long after its original airing window must not be counted as 'seasonal',
    even though it's COMPLETED."""
    entries = _fill(main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES)  # judged baseline
    entries.append(_sf_entry(
        entry_status="COMPLETED",
        season="WINTER", season_year=2026,
        start_date=date(2026, 11, 1),  # ~10 months after the WINTER 2026 window
    ))
    fake = SeasonalFakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_seasonal_follow_through(user_id=1)

    # Only the baseline fill entries were judged — the late binge never enters
    # followed_through/dropped_or_stalled/excluded_still_airing at all.
    assert result["judged"] == main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES
    assert result["followed_through"] == main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES
    assert result["excluded_still_airing"] == 0


def test_pre_buffer_before_season_start_still_counts(monkeypatch):
    """A show that starts airing a few days before its season's nominal start
    month (e.g. a late-December premiere counted as WINTER) and is watched
    from day one must still count as 'started while airing'."""
    entries = _fill(main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES - 1)
    entries.append(_sf_entry(
        entry_status="COMPLETED",
        season="WINTER", season_year=2026,
        start_date=date(2025, 12, 24),  # within PRE_BUFFER_DAYS of 2026-01-01
    ))
    fake = SeasonalFakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_seasonal_follow_through(user_id=1)

    assert result["judged"] == main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES


def test_classification_buckets(monkeypatch):
    entries = [
        _sf_entry(entry_status="COMPLETED"),
        _sf_entry(entry_status="REPEATING"),
        _sf_entry(entry_status="DROPPED"),
        _sf_entry(entry_status="WATCHING", anime_status="FINISHED"),  # stalled, never caught up
        _sf_entry(entry_status="PAUSED", anime_status="FINISHED"),    # stalled, never caught up
        _sf_entry(entry_status="WATCHING", anime_status="RELEASING"),  # still airing, no verdict
        _sf_entry(entry_status="PAUSED", anime_status="RELEASING"),    # still airing, no verdict
    ]
    fake = SeasonalFakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_seasonal_follow_through(user_id=1)

    # 5 judged entries (2 followed-through + 3 dropped/stalled) already clears
    # MIN_SAMPLES (5) on its own — the 2 still-airing rows must not be counted
    # toward either bucket.
    assert result["followed_through"] == 2  # COMPLETED + REPEATING
    assert result["dropped_or_stalled"] == 3  # DROPPED + 2x stalled-WATCHING/PAUSED against FINISHED
    assert result["excluded_still_airing"] == 2
    assert result["judged"] == 5
    assert result["rate"] == round(2 / 5 * 100)


def test_planning_entries_never_count_even_with_start_date(monkeypatch):
    entries = _fill(main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES)
    entries.append(_sf_entry(entry_status="PLANNING", start_date=date(2026, 1, 10)))
    fake = SeasonalFakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_seasonal_follow_through(user_id=1)

    assert result["judged"] == main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES


def test_movie_format_entries_are_excluded(monkeypatch):
    entries = _fill(main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES)
    entries.append(_sf_entry(entry_status="COMPLETED", format="MOVIE"))
    fake = SeasonalFakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_seasonal_follow_through(user_id=1)

    assert result["judged"] == main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES


def test_scoped_per_user(monkeypatch):
    entries = _fill(main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES, user_id=1)
    entries += _fill(main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES, user_id=2, entry_status="DROPPED")
    fake = SeasonalFakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result_1 = main._compute_seasonal_follow_through(user_id=1)
    result_2 = main._compute_seasonal_follow_through(user_id=2)

    assert result_1["followed_through"] == main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES
    assert result_1["dropped_or_stalled"] == 0
    assert result_2["followed_through"] == 0
    assert result_2["dropped_or_stalled"] == main.SEASONAL_FOLLOW_THROUGH_MIN_SAMPLES
