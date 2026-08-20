"""
Coverage for issue #222 — hour-of-day / day-of-week watch-activity breakdown on
/stats, Trakt-style, built entirely from library_entries.anilist_updated_at (the
only column on that row with real time-of-day resolution — start_date/finish_date
are DATE-only). No new schema.

No real DB is touched: app.main's `db` module is monkeypatched against a small
in-memory FakeDB modeling just the one query app.main._compute_watch_activity_times
issues (a plain SELECT of anilist_updated_at, filtered), matching the pattern used
elsewhere in this suite (test_taste_drift.py, test_rewatch_stats.py) — real
Postgres isn't stood up for unit tests. app.main.config is also monkeypatched to
control the user's configured timezone, the same setting /upcoming already reads
via config.get(user_id, "timezone").

Covers:
- correct day-of-week / hour-of-day bucketing against known UTC timestamps,
  converted to a configured non-UTC timezone (by_day is Monday=0..Sunday=6,
  matching /upcoming's week_grid convention; by_hour is local hour 0..23) —
  including a case that crosses a UTC day boundary once localized
- an invalid/unrecognized configured timezone name falls back to UTC, same as
  /upcoming
- entries with anilist_updated_at IS NULL are excluded
- PLANNING entries with progress = 0 are excluded (list-curation activity, not
  watching) — but a PLANNING entry with progress > 0 (e.g. re-planned after
  partial viewing) still counts, and any non-PLANNING status counts regardless
  of progress
- a brand-new account (zero entries) and a library where every entry is
  excluded by the two filters above both return None, not an error or an
  all-zero chart
- per-user scoping
"""

from datetime import datetime, timezone

import app.main as main


class FakeDB:
    """Models the single SELECT anilist_updated_at query
    _compute_watch_activity_times issues. Entries: list of dicts {user_id, status,
    progress, anilist_updated_at (datetime or None)}."""

    def __init__(self, entries=None):
        self.entries = entries or []

    def fetchall(self, query, params=None):
        if "SELECT anilist_updated_at" in query:
            (user_id,) = params
            out = []
            for e in self.entries:
                if e["user_id"] != user_id:
                    continue
                if e["anilist_updated_at"] is None:
                    continue
                if e["status"] == "PLANNING" and e["progress"] == 0:
                    continue
                out.append({"anilist_updated_at": e["anilist_updated_at"]})
            return out
        raise AssertionError(f"unexpected fetchall query: {query}")


class FakeConfig:
    """Models config.get(user_id, key) -> stored value or None, matching the
    signature _compute_watch_activity_times and /upcoming both call."""

    def __init__(self, timezone_by_user=None):
        self.timezone_by_user = timezone_by_user or {}

    def get(self, user_id, key):
        assert key == "timezone"
        return self.timezone_by_user.get(user_id)


def _entry(user_id=1, status="COMPLETED", progress=12, anilist_updated_at=None):
    return {
        "user_id": user_id,
        "status": status,
        "progress": progress,
        "anilist_updated_at": anilist_updated_at,
    }


def _ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, 0, tzinfo=timezone.utc)


def test_brand_new_account_returns_none(monkeypatch):
    fake = FakeDB([])
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main, "config", FakeConfig())

    result = main._compute_watch_activity_times(user_id=1)

    assert result is None


def test_all_entries_excluded_returns_none(monkeypatch):
    """Every entry is either missing a timestamp or a bare-PLANNING touch —
    nothing usable, so the section stays hidden rather than rendering all zeros."""
    fake = FakeDB([
        _entry(status="COMPLETED", anilist_updated_at=None),
        _entry(status="PLANNING", progress=0, anilist_updated_at=_ts(2026, 1, 5, 10)),
    ])
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main, "config", FakeConfig())

    result = main._compute_watch_activity_times(user_id=1)

    assert result is None


def test_buckets_by_day_and_hour_in_utc(monkeypatch):
    # 2026-08-17 is a Monday.
    fake = FakeDB([
        _entry(anilist_updated_at=_ts(2026, 8, 17, 20)),  # Mon 20:00
        _entry(anilist_updated_at=_ts(2026, 8, 17, 20)),  # Mon 20:00 (same bucket)
        _entry(anilist_updated_at=_ts(2026, 8, 16, 9)),   # Sun 09:00
    ])
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main, "config", FakeConfig({1: "UTC"}))

    result = main._compute_watch_activity_times(user_id=1)

    assert result is not None
    assert result["total"] == 3
    assert len(result["by_day"]) == 7
    assert len(result["by_hour"]) == 24
    assert result["by_day"][0] == 2   # Monday (index 0, datetime.weekday() convention)
    assert result["by_day"][6] == 1   # Sunday (index 6)
    assert sum(result["by_day"]) == 3
    assert result["by_hour"][20] == 2
    assert result["by_hour"][9] == 1
    assert sum(result["by_hour"]) == 3


def test_localizes_to_configured_timezone_across_day_boundary(monkeypatch):
    """A timestamp stored as 2026-08-17 23:30 UTC (Monday) is 2026-08-18 08:30
    in Asia/Tokyo (UTC+9) — a Tuesday. Both the day and hour buckets must reflect
    the localized instant, not the raw UTC one."""
    fake = FakeDB([
        _entry(anilist_updated_at=_ts(2026, 8, 17, 23, 30)),
    ])
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main, "config", FakeConfig({1: "Asia/Tokyo"}))

    result = main._compute_watch_activity_times(user_id=1)

    assert result["by_day"][1] == 1  # Tuesday (index 1) in Tokyo time
    assert result["by_day"][0] == 0  # not Monday, despite the UTC instant being Monday
    assert result["by_hour"][8] == 1


def test_invalid_timezone_falls_back_to_utc(monkeypatch):
    fake = FakeDB([
        _entry(anilist_updated_at=_ts(2026, 8, 17, 20)),
    ])
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main, "config", FakeConfig({1: "Not/AZone"}))

    result = main._compute_watch_activity_times(user_id=1)

    assert result["by_day"][0] == 1  # Monday, same as UTC — fallback applied
    assert result["by_hour"][20] == 1


def test_planning_with_progress_counts(monkeypatch):
    """A PLANNING entry with nonzero progress (e.g. partially watched, then
    re-planned) still reflects real viewing activity and must count."""
    fake = FakeDB([
        _entry(status="PLANNING", progress=3, anilist_updated_at=_ts(2026, 8, 17, 14)),
    ])
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main, "config", FakeConfig({1: "UTC"}))

    result = main._compute_watch_activity_times(user_id=1)

    assert result is not None
    assert result["total"] == 1
    assert result["by_hour"][14] == 1


def test_null_timestamp_excluded(monkeypatch):
    fake = FakeDB([
        _entry(anilist_updated_at=_ts(2026, 8, 17, 12)),
        _entry(anilist_updated_at=None),
    ])
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main, "config", FakeConfig({1: "UTC"}))

    result = main._compute_watch_activity_times(user_id=1)

    assert result["total"] == 1


def test_per_user_scoping(monkeypatch):
    fake = FakeDB([
        _entry(user_id=1, anilist_updated_at=_ts(2026, 8, 17, 12)),
        _entry(user_id=2, anilist_updated_at=_ts(2026, 8, 17, 13)),
        _entry(user_id=2, anilist_updated_at=_ts(2026, 8, 17, 14)),
    ])
    monkeypatch.setattr(main, "db", fake)
    monkeypatch.setattr(main, "config", FakeConfig({1: "UTC", 2: "UTC"}))

    result = main._compute_watch_activity_times(user_id=1)

    assert result["total"] == 1
    assert result["by_hour"][12] == 1
    assert result["by_hour"][13] == 0
