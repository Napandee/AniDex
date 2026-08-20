"""
Coverage for issue #191 — the rewatch queue/reminder surface on the /queue page.

Design decision under test: a completed show surfaces as a rewatch candidate when
`library_entries.finish_date` is at least REWATCH_REMINDER_MONTHS months in the
past. `finish_date` is synced straight from AniList's own `completedAt`
(scripts/sync_anilist.py), which AniList updates every time an entry is (re)marked
COMPLETED — including finishing a rewatch — so a single time-based check on that
field is sufficient to cover both "never rewatched, finished long ago" and
"rewatched recently" cases without any separate rewatch-recency lookup. See
app.main.rewatch_due() for the extracted, DB-free trigger-logic helper and the
comment above REWATCH_REMINDER_MONTHS in app/main.py for the fuller reasoning.

No real DB is touched — rewatch_due() is a pure function of (finish_date, months,
today), matching the pattern used elsewhere in this suite (see
test_rewatch_notes.py) of testing logic directly rather than standing up Postgres.
"""

from datetime import date, timedelta

import app.main as main


TODAY = date(2026, 8, 20)


def days_ago(n):
    return TODAY - timedelta(days=n)


def test_show_finished_long_ago_with_no_rewatch_surfaces():
    # Finished 8 months ago (240 days), well past the 6-month/180-day default
    # threshold, never rewatched (repeat_count would be 0, but rewatch_due()
    # only looks at finish_date since AniList's completedAt already reflects
    # the most recent completion regardless of rewatch count).
    finish_date = days_ago(240)
    assert main.rewatch_due(finish_date, today=TODAY) is True


def test_show_finished_recently_does_not_surface():
    # Finished 2 months ago (60 days) — well under the threshold.
    finish_date = days_ago(60)
    assert main.rewatch_due(finish_date, today=TODAY) is False


def test_show_rewatched_recently_does_not_surface():
    # Originally completed a year ago, but AniList's completedAt (and therefore
    # this app's finish_date) was updated when the most recent rewatch finished
    # a month ago — the same field a first-time completion uses, per the design
    # decision above. A recent rewatch must not surface again immediately.
    finish_date = days_ago(30)
    assert main.rewatch_due(finish_date, today=TODAY) is False


def test_show_finished_exactly_at_threshold_surfaces():
    # >= threshold (not strictly >) — a boundary check on the exact cutoff.
    finish_date = TODAY - timedelta(days=main.REWATCH_REMINDER_MONTHS * 30)
    assert main.rewatch_due(finish_date, today=TODAY) is True


def test_show_one_day_under_threshold_does_not_surface():
    finish_date = TODAY - timedelta(days=main.REWATCH_REMINDER_MONTHS * 30 - 1)
    assert main.rewatch_due(finish_date, today=TODAY) is False


def test_no_finish_date_never_surfaces():
    # Shouldn't happen for a COMPLETED entry in practice, but guards against a
    # None finish_date (e.g. a pre-sync row) ever raising instead of just
    # excluding the show.
    assert main.rewatch_due(None, today=TODAY) is False


def test_custom_months_threshold_respected():
    finish_date = days_ago(100)
    assert main.rewatch_due(finish_date, months=3, today=TODAY) is True
    assert main.rewatch_due(finish_date, months=6, today=TODAY) is False
