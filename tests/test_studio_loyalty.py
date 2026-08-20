"""
Coverage for issue #223 — "studio loyalty" view on /stats showing per-studio
average score vs. volume watched (completed titles), derived entirely from
anime.studios + library_entries.score/status (no new schema, no snapshot table).

No real DB is touched: app.main's `db` module is monkeypatched against a small
in-memory FakeDB modeling the one query app.main._compute_studio_loyalty issues
(group by main-studio name, COUNT/COUNT FILTER/AVG FILTER on scored entries),
matching the pattern used elsewhere in this suite (test_taste_drift.py) — real
Postgres isn't stood up for unit tests.

Covers:
- only isMain=true studio credits count (matches scripts/run_recommender.py's
  own studio-matching convention)
- only COMPLETED entries count toward volume (WATCHING/PLANNING/DROPPED excluded
  even if a finish_date/score happens to be set)
- STUDIO_LOYALTY_MIN_TITLES gating: studios below the threshold are excluded from
  the output and counted in excluded_low_volume
- a studio that clears the title threshold but has zero *scored* completions is
  excluded separately (excluded_unscored) — there's no average to plot
- correct per-studio average-score computation, restricted to scored (score > 0)
  entries only
- output sorted by avg_score desc, then title_count desc
- per-user scoping
- a brand-new account / a library with no isMain studio credits at all returns
  None, not an error
"""

import app.main as main


class FakeDB:
    """Models the single query _compute_studio_loyalty issues: group COMPLETED
    library_entries by main-studio name, computing title_count, scored_count
    (score IS NOT NULL AND score > 0), and avg_score over just the scored ones.
    Entries: list of dicts {user_id, status, score (float or None),
    studios: [{"name": str, "isMain": bool}, ...]}."""

    def __init__(self, entries=None):
        self.entries = entries or []

    def fetchall(self, query, params=None):
        if "GROUP BY studio_elem->>'name'" in query:
            (user_id,) = params
            agg: dict[str, dict] = {}
            for e in self.entries:
                if e["user_id"] != user_id or e["status"] != "COMPLETED":
                    continue
                for studio in e.get("studios") or []:
                    if not studio.get("isMain"):
                        continue
                    name = studio["name"]
                    bucket = agg.setdefault(
                        name, {"title_count": 0, "scored_count": 0, "score_sum": 0.0}
                    )
                    bucket["title_count"] += 1
                    score = e.get("score")
                    if score is not None and score > 0:
                        bucket["scored_count"] += 1
                        bucket["score_sum"] += score
            rows = []
            for name, b in agg.items():
                avg_score = (
                    b["score_sum"] / b["scored_count"] if b["scored_count"] > 0 else None
                )
                rows.append({
                    "studio": name,
                    "title_count": b["title_count"],
                    "scored_count": b["scored_count"],
                    "avg_score": avg_score,
                })
            return rows
        raise AssertionError(f"unexpected fetchall query: {query}")


def _entry(user_id=1, status="COMPLETED", score=None, studios=None):
    return {"user_id": user_id, "status": status, "score": score, "studios": studios or []}


def _main_studio(name):
    return [{"name": name, "isMain": True}]


def test_brand_new_account_returns_none(monkeypatch):
    fake = FakeDB([])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_studio_loyalty(user_id=1)

    assert result is None


def test_no_main_studio_credits_returns_none(monkeypatch):
    """Only non-main studio credits exist (e.g. a co-producer) -- nothing to
    attribute, matching scripts/run_recommender.py's own isMain-only convention."""
    fake = FakeDB([
        _entry(score=4.0, studios=[{"name": "Some Co-Producer", "isMain": False}]),
    ])
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_studio_loyalty(user_id=1)

    assert result is None


def test_below_title_threshold_excluded(monkeypatch):
    """Fewer than STUDIO_LOYALTY_MIN_TITLES completed titles from a studio ->
    excluded from the output, counted in excluded_low_volume, never plotted."""
    n = main.STUDIO_LOYALTY_MIN_TITLES - 1
    entries = [_entry(score=5.0, studios=_main_studio("Tiny Studio")) for _ in range(n)]
    fake = FakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_studio_loyalty(user_id=1)

    assert result is None  # nothing else qualifies either
    # Re-run with a qualifying studio alongside to check the counter directly.
    entries += [
        _entry(score=4.0, studios=_main_studio("Big Studio"))
        for _ in range(main.STUDIO_LOYALTY_MIN_TITLES)
    ]
    fake = FakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_studio_loyalty(user_id=1)

    assert result is not None
    names = [s["studio"] for s in result["studios"]]
    assert "Tiny Studio" not in names
    assert "Big Studio" in names
    assert result["excluded_low_volume"] == 1


def test_at_title_threshold_included(monkeypatch):
    """Exactly STUDIO_LOYALTY_MIN_TITLES completed+rated titles -> included (the
    cutoff is inclusive)."""
    n = main.STUDIO_LOYALTY_MIN_TITLES
    entries = [_entry(score=4.5, studios=_main_studio("Threshold Studio")) for _ in range(n)]
    fake = FakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_studio_loyalty(user_id=1)

    assert result is not None
    assert result["studios"][0]["studio"] == "Threshold Studio"
    assert result["studios"][0]["title_count"] == n


def test_qualifying_but_unscored_studio_excluded(monkeypatch):
    """A studio clears the title-count threshold but has zero scored completions
    -- there's no average to plot, so it's excluded (separately tracked from the
    low-volume exclusion) rather than shown as a 0.0 average."""
    n = main.STUDIO_LOYALTY_MIN_TITLES
    entries = [_entry(score=None, studios=_main_studio("Unrated Studio")) for _ in range(n)]
    entries += [
        _entry(score=4.0, studios=_main_studio("Rated Studio")) for _ in range(n)
    ]
    fake = FakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_studio_loyalty(user_id=1)

    assert result is not None
    names = [s["studio"] for s in result["studios"]]
    assert "Unrated Studio" not in names
    assert "Rated Studio" in names
    assert result["excluded_unscored"] == 1


def test_average_score_computed_over_scored_entries_only(monkeypatch):
    """A studio with some unscored completions alongside scored ones: title_count
    reflects all completions, but avg_score is computed only from the scored
    subset -- an unscored entry must never silently pull the average toward 0."""
    n = main.STUDIO_LOYALTY_MIN_TITLES
    entries = [_entry(score=None, studios=_main_studio("Mixed Studio"))]
    entries += [_entry(score=5.0, studios=_main_studio("Mixed Studio")) for _ in range(n)]
    fake = FakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_studio_loyalty(user_id=1)

    studio = result["studios"][0]
    assert studio["title_count"] == n + 1
    assert studio["scored_count"] == n
    assert studio["avg_score"] == 5.0


def test_score_zero_treated_as_unrated(monkeypatch):
    """AniList's sentinel 'no score' value (0) must not count as a real rating,
    matching the le.score > 0 convention already used elsewhere in this file
    (e.g. the mean_score headline)."""
    n = main.STUDIO_LOYALTY_MIN_TITLES
    entries = [_entry(score=0.0, studios=_main_studio("Zero Studio")) for _ in range(n)]
    fake = FakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_studio_loyalty(user_id=1)

    assert result is None  # all "scores" are the 0 sentinel -> excluded_unscored


def test_non_completed_status_excluded(monkeypatch):
    """A WATCHING/DROPPED entry must never contribute to volume or average, even
    if it happens to carry a score."""
    n = main.STUDIO_LOYALTY_MIN_TITLES
    entries = [_entry(status="COMPLETED", score=5.0, studios=_main_studio("Studio A")) for _ in range(n)]
    entries += [_entry(status="WATCHING", score=5.0, studios=_main_studio("Studio A"))]
    entries += [_entry(status="DROPPED", score=1.0, studios=_main_studio("Studio A"))]
    fake = FakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_studio_loyalty(user_id=1)

    assert result["studios"][0]["title_count"] == n
    assert result["studios"][0]["avg_score"] == 5.0


def test_multiple_main_studios_each_credited(monkeypatch):
    """A title co-credited to two main studios contributes to both -- matches
    run_recommender.py's own multi-main-studio handling."""
    n = main.STUDIO_LOYALTY_MIN_TITLES
    dual = [{"name": "Studio A", "isMain": True}, {"name": "Studio B", "isMain": True}]
    entries = [_entry(score=4.0, studios=dual) for _ in range(n)]
    fake = FakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_studio_loyalty(user_id=1)

    names = {s["studio"] for s in result["studios"]}
    assert names == {"Studio A", "Studio B"}
    for s in result["studios"]:
        assert s["title_count"] == n


def test_sorted_by_avg_score_desc_then_title_count_desc(monkeypatch):
    n = main.STUDIO_LOYALTY_MIN_TITLES
    entries = [_entry(score=3.0, studios=_main_studio("Mid Studio")) for _ in range(n)]
    entries += [_entry(score=5.0, studios=_main_studio("Top Studio")) for _ in range(n)]
    entries += [_entry(score=1.0, studios=_main_studio("Low Studio")) for _ in range(n)]
    fake = FakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_studio_loyalty(user_id=1)

    ordered = [s["studio"] for s in result["studios"]]
    assert ordered == ["Top Studio", "Mid Studio", "Low Studio"]


def test_scoped_per_user(monkeypatch):
    """Another user's completed titles must never leak into this user's studio
    loyalty data."""
    n = main.STUDIO_LOYALTY_MIN_TITLES
    entries = [_entry(user_id=1, score=5.0, studios=_main_studio("Studio A")) for _ in range(n)]
    entries += [_entry(user_id=2, score=1.0, studios=_main_studio("Studio B")) for _ in range(n)]
    fake = FakeDB(entries)
    monkeypatch.setattr(main, "db", fake)

    result = main._compute_studio_loyalty(user_id=1)

    names = [s["studio"] for s in result["studios"]]
    assert names == ["Studio A"]
