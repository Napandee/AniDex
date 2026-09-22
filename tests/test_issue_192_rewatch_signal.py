"""
Coverage for issue #192 — repeat_count as a taste signal in build_taste_profile().

Measured 2026-09-10 against the live library: 77% of scored COMPLETED entries
(94/122) score 5/5, so under the existing `score / 5` weighting they all sit at
an identical 1.0 and the profile cannot tell any of them apart. Every one of the
20 entries with repeat_count >= 2 scored 5/5 — rewatching is a genuine
preference signal, not comfort-watching noise — and it is one of the few
independent signals available that differentiates inside that saturated band.

Two behaviours, per the rescoped issue:
  1. tie-break inside the 5-star band: a rewatched 5 outweighs a never-rewatched 5
  2. stand in for a missing score: an unscored-but-rewatched COMPLETED entry no
     longer gets the same flat COMPLETED_UNSCORED_WEIGHT as an unscored one that
     was never rewatched

Magnitude is bounded on purpose: the bonus is capped at REWATCH_CAP rewatches
(all but one library entry sit at <= 3; the lone rc=6 must not dominate) and
sized so a rewatched 5 still lands in the same neighbourhood as other 5s rather
than swamping them.

Pure fake-connection coverage — no DB, no network. These are the first tests
build_taste_profile() has had.
"""

import run_recommender as rr


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        assert "FROM library_entries" in query

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._rows)


def _entry(genre, status="COMPLETED", score=5, repeat_count=0):
    return {
        "genres": [genre],
        "tags": [],
        "studios": [],
        "status": status,
        "score": score,
        "repeat_count": repeat_count,
    }


def test_rewatched_five_star_outweighs_never_rewatched_five_star():
    """The saturated-band tie-break. Both are 5/5 and both currently contribute an
    identical 1.0, so today `Rewatched` and `Once` come out equal."""
    conn = _FakeConn([
        _entry("Rewatched", score=5, repeat_count=3),
        _entry("Once", score=5, repeat_count=0),
    ])

    profile = rr.build_taste_profile(conn)

    assert profile["genres"]["Rewatched"] > profile["genres"]["Once"]


# The tests below are regression guards on the acceptance criteria. They passed
# on first run against the implementation above (the single `weight += bonus`
# line covers every branch), so they are not TDD-driven — they exist so a later
# edit that breaks one of these properties fails loudly.

def test_unscored_but_rewatched_beats_unscored_never_rewatched():
    """Acceptance criterion 2: an unscored COMPLETED entry with rewatches must not
    receive the same flat COMPLETED_UNSCORED_WEIGHT as one never rewatched."""
    conn = _FakeConn([
        _entry("Rewatched", score=None, repeat_count=3),
        _entry("Once", score=None, repeat_count=0),
    ])

    profile = rr.build_taste_profile(conn)

    assert profile["genres"]["Once"] == rr.COMPLETED_UNSCORED_WEIGHT
    assert profile["genres"]["Rewatched"] > rr.COMPLETED_UNSCORED_WEIGHT


def test_rewatch_bonus_is_capped_so_one_title_cannot_dominate():
    """The lone rc=6 entry in the real library (Sword Art Online) must weigh the
    same as an rc=3 entry — the bonus is flat past REWATCH_CAP."""
    assert rr._rewatch_bonus(6) == rr._rewatch_bonus(3)
    assert rr._rewatch_bonus(3) == rr.REWATCH_BONUS_MAX
    assert rr._rewatch_bonus(0) == 0.0
    assert rr._rewatch_bonus(None) == 0.0


def test_score_still_wins_across_bands_rewatch_only_breaks_ties_within_one():
    """A rewatched 3/5 (0.6 + 0.25 = 0.85) must NOT overtake a never-rewatched 5/5
    (1.0). If it did, the bonus would be a scoring change, not a tie-breaker —
    which is exactly the thing #192's rescoping ruled out."""
    conn = _FakeConn([
        _entry("RewatchedThree", score=3, repeat_count=3),
        _entry("PlainFive", score=5, repeat_count=0),
    ])

    profile = rr.build_taste_profile(conn)

    assert profile["genres"]["PlainFive"] > profile["genres"]["RewatchedThree"]


def test_library_with_no_rewatches_is_byte_identical_to_old_behaviour():
    """Acceptance criterion 4: repeat_count 0 everywhere must reproduce the
    pre-#192 weights exactly — score/5 for scored, the flat constant for not."""
    conn = _FakeConn([
        _entry("A", score=5, repeat_count=0),
        _entry("B", score=2, repeat_count=0),
        _entry("C", score=None, repeat_count=0),
    ])

    profile = rr.build_taste_profile(conn)

    assert profile["genres"]["A"] == 1.0
    assert profile["genres"]["B"] == 0.4
    assert profile["genres"]["C"] == rr.COMPLETED_UNSCORED_WEIGHT


def test_watching_and_planning_ignore_repeat_count():
    """repeat_count only means something once you have finished and gone back.
    A WATCHING or PLANNING row with a stray non-zero repeat_count must keep its
    fixed status weight."""
    conn = _FakeConn([
        _entry("W", status="WATCHING", score=None, repeat_count=3),
        _entry("P", status="PLANNING", score=None, repeat_count=3),
    ])

    profile = rr.build_taste_profile(conn)

    assert profile["genres"]["W"] == rr.WATCHING_WEIGHT
    assert profile["genres"]["P"] == rr.PLANNING_WEIGHT
