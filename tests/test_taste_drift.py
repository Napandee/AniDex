"""
Coverage for issue #176 — "taste drift" view on /stats showing how the genre mix
of a user's COMPLETED library has shifted over time, derived entirely from
library_entries.finish_date + anime.genres (no new schema, no snapshot table),
per the query pattern validated in #77's comment thread.

No real DB is touched: app.main's `db` module is monkeypatched against a small
in-memory FakeDB modeling just enough of the two queries
app.main._compute_taste_drift issues (a totals lookup, then the bucket/genre
breakdown), matching the pattern used elsewhere in this suite
(test_rewatch_stats.py) — real Postgres isn't stood up for unit tests.

Covers:
- correct bucketing/counting of genre counts per time bucket against known data
- the quarterly-vs-yearly threshold logic at both sides of
  TASTE_DRIFT_MIN_COMPLETED_FOR_QUARTERLY
- missing-finish_date entries are excluded from bucketing but still counted in
  total_completed (coverage transparency), not silently dropped or miscounted
- a brand-new account (zero COMPLETED entries) and a completed-but-never-dated
  library (all finish_date NULL) both return None, not an error
- per-user scoping
- genres beyond TASTE_DRIFT_TOP_GENRES collapse into "Other" rather than
  producing an unbounded series/legend
"""

from datetime import date

import app.main as main


class FakeDB:
    """Models just enough of the two queries _compute_taste_drift issues:
    (1) a totals lookup (COUNT total vs. COUNT FILTER usable), and (2) the
    bucket/genre breakdown grouped by date_trunc(granularity, finish_date) and
    genre. Entries: list of dicts {user_id, status, finish_date (date or None),
    genres (list[str])}."""

    def __init__(self, entries=None):
        self.entries = entries or []

    def fetchone(self, query, params=None):
        if "total_completed" in query:
            (user_id,) = params
            completed = [
                e for e in self.entries
                if e["user_id"] == user_id and e["status"] == "COMPLETED"
            ]
            usable = [e for e in completed if e["finish_date"] is not None]
            return {"total_completed": len(completed), "usable_completed": len(usable)}
        raise AssertionError(f"unexpected fetchone query: {query}")

    def fetchall(self, query, params=None):
        if "GROUP BY bucket, genre" in query:
            granularity, user_id = params
            counts: dict[tuple, int] = {}
            for e in self.entries:
                if (
                    e["user_id"] != user_id
                    or e["status"] != "COMPLETED"
                    or e["finish_date"] is None
                ):
                    continue
                bucket = _truncate(e["finish_date"], granularity)
                for genre in e["genres"]:
                    key = (bucket, genre)
                    counts[key] = counts.get(key, 0) + 1
            rows = [
                {"bucket": bucket, "genre": genre, "cnt": cnt}
                for (bucket, genre), cnt in counts.items()
            ]
            rows.sort(key=lambda r: (r["bucket"], -r["cnt"]))
            return rows
        raise AssertionError(f"unexpected fetchall query: {query}")


def _truncate(d: date, granularity: str) -> date:
    if granularity == "year":
        return date(d.year, 1, 1)
    q_month = ((d.month - 1) // 3) * 3 + 1
    return date(d.year, q_month, 1)


def _entry(user_id=1, status="COMPLETED", finish_date=None, genres=None):
    return {
        "user_id": user_id,
        "status": status,
        "finish_date": finish_date,
        "genres": genres or [],
    }


def _completed_titles(n, user_id=1, start_year=2024, genres=("Action",)):
    """n COMPLETED entries with a usable finish_date, spread across n distinct
    months starting Jan start_year, each tagged with `genres`."""
    out = []
    for i in range(n):
        year = start_year + (i // 12)
        month = (i % 12) + 1
        out.append(_entry(user_id=user_id, finish_date=date(year, month, 1), genres=list(genres)))
    return out


def test_brand_new_account_returns_none(monkeypatch):
    fake = FakeDB([])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_taste_drift(user_id=1)

    assert result is None


def test_completed_but_no_finish_dates_returns_none(monkeypatch):
    """All COMPLETED entries exist but none has a finish_date set — nothing to
    bucket, so the section stays hidden entirely rather than rendering empty."""
    fake = FakeDB([
        _entry(finish_date=None, genres=["Action"]),
        _entry(finish_date=None, genres=["Drama"]),
    ])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_taste_drift(user_id=1)

    assert result is None


def test_below_threshold_uses_yearly_buckets(monkeypatch):
    """Just under TASTE_DRIFT_MIN_COMPLETED_FOR_QUARTERLY usable completions ->
    yearly granularity."""
    n = main.TASTE_DRIFT_MIN_COMPLETED_FOR_QUARTERLY - 1
    fake = FakeDB(_completed_titles(n))
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_taste_drift(user_id=1)

    assert result is not None
    assert result["granularity"] == "year"
    assert result["usable_completed"] == n


def test_at_threshold_uses_quarterly_buckets(monkeypatch):
    """Exactly TASTE_DRIFT_MIN_COMPLETED_FOR_QUARTERLY usable completions ->
    quarterly granularity (the cutoff is inclusive)."""
    n = main.TASTE_DRIFT_MIN_COMPLETED_FOR_QUARTERLY
    fake = FakeDB(_completed_titles(n))
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_taste_drift(user_id=1)

    assert result is not None
    assert result["granularity"] == "quarter"
    assert result["usable_completed"] == n


def test_above_threshold_uses_quarterly_buckets(monkeypatch):
    n = main.TASTE_DRIFT_MIN_COMPLETED_FOR_QUARTERLY + 20
    fake = FakeDB(_completed_titles(n))
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_taste_drift(user_id=1)

    assert result["granularity"] == "quarter"


def test_missing_finish_date_excluded_from_buckets_but_counted_in_total(monkeypatch):
    """Entries with no finish_date must never appear in a bucket, but
    total_completed should still reflect them so the UI can show real coverage
    (e.g. "8 of 10 completed titles ...") rather than silently understating the
    library size."""
    fake = FakeDB([
        _entry(finish_date=date(2024, 1, 15), genres=["Action"]),
        _entry(finish_date=date(2024, 2, 10), genres=["Action"]),
        _entry(finish_date=None, genres=["Drama"]),
    ])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_taste_drift(user_id=1)

    assert result["total_completed"] == 3
    assert result["usable_completed"] == 2
    total_bucketed = sum(
        count for b in result["buckets"] for count in b["counts"].values()
    )
    assert total_bucketed == 2  # the NULL-finish_date entry never lands in a bucket


def test_bucket_counts_and_labels_quarterly(monkeypatch):
    """Two titles in the same quarter, one genre each, plus enough padding
    entries to clear the quarterly threshold; verifies per-bucket per-genre
    counts and the "YYYY Qn" label format."""
    entries = _completed_titles(main.TASTE_DRIFT_MIN_COMPLETED_FOR_QUARTERLY - 2, start_year=2020)
    entries += [
        _entry(finish_date=date(2024, 2, 5), genres=["Isekai"]),
        _entry(finish_date=date(2024, 3, 20), genres=["Isekai"]),
    ]
    fake = FakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_taste_drift(user_id=1)

    assert result["granularity"] == "quarter"
    q1_2024 = next(b for b in result["buckets"] if b["label"] == "2024 Q1")
    assert q1_2024["counts"]["Isekai"] == 2


def test_genres_beyond_top_n_collapse_into_other(monkeypatch):
    """More distinct genres than TASTE_DRIFT_TOP_GENRES exist -> the long tail
    collapses into a single "Other" series instead of an unbounded one."""
    entries = []
    # One heavily-repeated genre per "rank" so most_common ordering is stable,
    # plus enough total volume to clear the quarterly threshold.
    n_genres = main.TASTE_DRIFT_TOP_GENRES + 3
    for rank in range(n_genres):
        genre = f"Genre{rank:02d}"
        # Higher rank number = fewer occurrences, so genre ordering by count is
        # deterministic and the last few are guaranteed to fall outside top N.
        occurrences = (n_genres - rank) + 5
        for i in range(occurrences):
            entries.append(_entry(finish_date=date(2024, (i % 12) + 1, 1), genres=[genre]))
    fake = FakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_taste_drift(user_id=1)

    assert len(result["genres"]) == main.TASTE_DRIFT_TOP_GENRES + 1  # + "Other"
    assert "Other" in result["genres"]
    other_total = sum(b["counts"].get("Other", 0) for b in result["buckets"])
    assert other_total > 0


def test_scoped_per_user(monkeypatch):
    """Another user's completed titles must never leak into this user's drift
    data or its totals."""
    fake = FakeDB([
        _entry(user_id=1, finish_date=date(2024, 1, 1), genres=["Action"]),
        _entry(user_id=2, finish_date=date(2024, 1, 1), genres=["Drama"]),
    ])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_taste_drift(user_id=1)

    assert result["total_completed"] == 1
    assert result["genres"] == ["Action"]


def test_non_completed_status_excluded(monkeypatch):
    """A WATCHING/PLANNING/DROPPED entry with a finish_date set (edge case) must
    never contribute — only COMPLETED status counts toward the taste profile,
    per the issue's explicit out-of-scope note on WATCHING/PLANNING."""
    fake = FakeDB([
        _entry(status="COMPLETED", finish_date=date(2024, 1, 1), genres=["Action"]),
        _entry(status="DROPPED", finish_date=date(2024, 1, 1), genres=["Drama"]),
        _entry(status="WATCHING", finish_date=date(2024, 1, 1), genres=["Comedy"]),
    ])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_taste_drift(user_id=1)

    assert result["total_completed"] == 1
    assert result["genres"] == ["Action"]
