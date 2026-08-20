"""
Coverage for issue #189 — surfacing rewatch data ("most rewatched" list +
rewatch-count distribution) on /stats, built entirely from the existing
library_entries.repeat_count column (already correctly populated from AniList's
`repeat` field, see sync_anilist.py — not this issue's concern).

No real DB is touched: app.main's `db` module is monkeypatched against a small
in-memory FakeDB modeling just enough of the `library_entries` JOIN `anime` query
shape that app.main._compute_rewatch_stats issues (see FakeDB.fetchall below),
matching the pattern used elsewhere in this suite (test_rewatch_notes.py,
test_import_personal_notes.py) — real Postgres isn't stood up for unit tests.

Covers:
- ranking/ordering of the "most rewatched" list (repeat_count desc, title asc tiebreak)
- rewatch-count distribution bucketing, including 3+ aggregation
- a user with zero rewatches (but some completed titles) is handled gracefully,
  not as an error
- a user with zero completed titles at all (brand-new account) is handled gracefully
- only COMPLETED/REPEATING entries count — a PLANNING/WATCHING/DROPPED entry must
  never pad either the distribution or the most-rewatched list
- the most-rewatched list is capped at REWATCH_MOST_REWATCHED_LIMIT
- per-user scoping — another user's entries never leak in
"""

import app.main as main


class FakeDB:
    """Models just enough of the `library_entries JOIN anime` query shape that
    _compute_rewatch_stats issues to exercise its Python-side bucketing/ranking
    logic against realistic filtering (user_id scoping, status IN (...), the
    repeat_count > 0 predicate) without a real Postgres connection."""

    def __init__(self, entries=None):
        # entries: list of dicts {user_id, title, status, repeat_count}
        self.entries = entries or []

    def fetchall(self, query, params=None):
        if "GROUP BY bucket" in query:
            (user_id,) = params
            buckets: dict[str, int] = {}
            for e in self.entries:
                if e["user_id"] != user_id or e["status"] not in ("COMPLETED", "REPEATING"):
                    continue
                rc = e["repeat_count"]
                b = "3+" if rc >= 3 else str(rc)
                buckets[b] = buckets.get(b, 0) + 1
            return [{"bucket": b, "cnt": c} for b, c in buckets.items()]

        if "ORDER BY le.repeat_count DESC" in query:
            user_id, limit = params
            rows = [
                e for e in self.entries
                if e["user_id"] == user_id
                and e["repeat_count"] > 0
                and e["status"] in ("COMPLETED", "REPEATING")
            ]
            rows.sort(key=lambda e: (-e["repeat_count"], e["title"]))
            return [{"title": e["title"], "count": e["repeat_count"]} for e in rows[:limit]]

        raise AssertionError(f"unexpected query: {query}")


def _entry(user_id=1, title="", status="COMPLETED", repeat_count=0):
    return {"user_id": user_id, "title": title, "status": status, "repeat_count": repeat_count}


def test_user_with_no_rewatches_returns_empty_list_not_error(monkeypatch):
    """Every title completed exactly once (repeat_count 0) — the acceptance
    criteria's explicit "handles zero-rewatch users gracefully" case. Distribution
    should still report the completed count in the "0" bucket; the most-rewatched
    list should be empty, not an error or a list full of zero-count rows."""
    fake = FakeDB([
        _entry(title="Show A", repeat_count=0),
        _entry(title="Show B", repeat_count=0),
    ])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_rewatch_stats(user_id=1)

    assert result["most_rewatched"] == []
    assert result["total_rewatched_titles"] == 0
    assert result["total_completed_titles"] == 2
    dist = {d["bucket"]: d["count"] for d in result["distribution"]}
    assert dist == {"0": 2, "1": 0, "2": 0, "3+": 0}


def test_brand_new_account_with_no_completed_titles(monkeypatch):
    """No library entries at all yet — must not crash, and every bucket (plus
    total_completed_titles) should read as zero."""
    fake = FakeDB([])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_rewatch_stats(user_id=1)

    assert result["most_rewatched"] == []
    assert result["total_completed_titles"] == 0
    assert result["total_rewatched_titles"] == 0
    assert all(d["count"] == 0 for d in result["distribution"])


def test_most_rewatched_ranking_and_tiebreak(monkeypatch):
    """Ranked by repeat_count descending; ties broken alphabetically by title,
    matching the ORDER BY le.repeat_count DESC, title ASC in the real query."""
    fake = FakeDB([
        _entry(title="Zeta Rewatch", repeat_count=2),
        _entry(title="Alpha Rewatch", repeat_count=2),
        _entry(title="Once Only", repeat_count=1),
        _entry(title="Never Rewatched", repeat_count=0),
    ])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_rewatch_stats(user_id=1)

    titles_in_order = [r["title"] for r in result["most_rewatched"]]
    assert titles_in_order == ["Alpha Rewatch", "Zeta Rewatch", "Once Only"]
    assert result["most_rewatched"][0]["count"] == 2


def test_distribution_buckets_three_plus_together(monkeypatch):
    """repeat_count 3 and repeat_count 7 both land in the same "3+" bucket."""
    fake = FakeDB([
        _entry(title="A", repeat_count=0),
        _entry(title="B", repeat_count=1),
        _entry(title="C", repeat_count=2),
        _entry(title="D", repeat_count=3),
        _entry(title="E", repeat_count=7),
    ])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_rewatch_stats(user_id=1)

    dist = {d["bucket"]: d["count"] for d in result["distribution"]}
    assert dist == {"0": 1, "1": 1, "2": 1, "3+": 2}
    assert result["total_completed_titles"] == 5
    assert result["total_rewatched_titles"] == 4


def test_non_completed_statuses_are_excluded(monkeypatch):
    """A repeat_count > 0 on a PLANNING/WATCHING/DROPPED row (edge case — AniList
    wouldn't normally produce this, but the query must not trust that) must not
    show up in either the distribution or the most-rewatched list."""
    fake = FakeDB([
        _entry(title="Completed Rewatch", status="COMPLETED", repeat_count=2),
        _entry(title="Still Watching", status="WATCHING", repeat_count=1),
        _entry(title="Planned Only", status="PLANNING", repeat_count=0),
        _entry(title="Dropped After Rewatch", status="DROPPED", repeat_count=1),
        _entry(title="Mid Rewatch", status="REPEATING", repeat_count=1),
    ])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_rewatch_stats(user_id=1)

    titles = {r["title"] for r in result["most_rewatched"]}
    assert titles == {"Completed Rewatch", "Mid Rewatch"}
    # Only the COMPLETED + REPEATING rows count toward the distribution total.
    assert result["total_completed_titles"] == 2


def test_most_rewatched_list_is_capped_at_limit(monkeypatch):
    """More than REWATCH_MOST_REWATCHED_LIMIT rewatched titles exist — only the
    top N by repeat_count come back."""
    entries = [
        _entry(title=f"Show {i:02d}", repeat_count=i + 1)
        for i in range(main.REWATCH_MOST_REWATCHED_LIMIT + 5)
    ]
    fake = FakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_rewatch_stats(user_id=1)

    assert len(result["most_rewatched"]) == main.REWATCH_MOST_REWATCHED_LIMIT
    # Highest repeat_count entries win the cap.
    assert result["most_rewatched"][0]["title"] == f"Show {main.REWATCH_MOST_REWATCHED_LIMIT + 4:02d}"


def test_scoped_per_user(monkeypatch):
    """Another user's rewatch data must never leak into this user's stats."""
    fake = FakeDB([
        _entry(user_id=1, title="My Show", repeat_count=3),
        _entry(user_id=2, title="Someone Else's Show", repeat_count=9),
    ])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_rewatch_stats(user_id=1)

    assert [r["title"] for r in result["most_rewatched"]] == ["My Show"]
    assert result["total_completed_titles"] == 1
