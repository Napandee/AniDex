"""
Coverage for issue #190 — exposing `library_entries.repeat_count` as a sort/filter
option in the library view — extended by issue #330, which merged the separate
Watching/Repeating tabs into one and updated the rewatch filter's criterion to
also catch a rewatch still in progress (status REPEATING), not just a completed
one (repeat_count >= 1), plus swapped the old two-button ("All"/"Rewatched") pill
pair for a single toggle switch. All of the actual sort/filter logic lives
client-side in vanilla JS (app/static/script.js's sortLibrary/applyLibraryFilters,
plus the per-page rewatch-filter IIFE in app/templates/library.html) — this app
deliberately has no frontend build step or JS test runner (see CLAUDE.md), so this
file follows the same static-content-assertion approach already used by
test_i18n.py for the same reason, plus a pure-Python re-implementation of the two
behaviors (sort comparator, filter predicate) to pin down their semantics.

No Postgres/app import needed for the static checks — they only read the template
and script.js source. The behavioral tests re-implement the JS in Python rather
than execute real JS (no jsdom/node runtime is part of this repo's toolchain).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIBRARY_HTML = (REPO_ROOT / "app" / "templates" / "library.html").read_text(encoding="utf-8")
SCRIPT_JS = (REPO_ROOT / "app" / "static" / "script.js").read_text(encoding="utf-8")


# ── Static wiring checks ─────────────────────────────────────────────────────────

def test_card_renders_repeat_count_data_attribute():
    assert 'data-repeat-count="{{ e.repeat_count or 0 }}"' in LIBRARY_HTML


def test_sort_group_has_rewatches_button():
    assert re.search(r'<button class="sort-btn"[^>]*data-sort="rewatches"', LIBRARY_HTML)


def test_filter_row_has_rewatch_toggle_switch():
    """Issue #330 — the old two-button ("All"/"Rewatched") pill pair became a
    single toggle switch; only one [data-rewatch-filter] *element* exists now
    (the JS selector string itself also contains the attribute name once more,
    so this counts real <button ...> tags rather than raw substring count)."""
    assert len(re.findall(r'<button[^>]*\bdata-rewatch-filter="[^"]*"', LIBRARY_HTML)) == 1
    assert 'data-rewatch-filter=""' in LIBRARY_HTML
    assert 'class="rewatch-toggle"' in LIBRARY_HTML
    assert 'role="switch"' in LIBRARY_HTML


def test_card_renders_status_data_attribute():
    """Issue #330 — needed client-side so the filter can also catch an
    in-progress rewatch (status REPEATING), not just a completed one."""
    assert 'data-status="{{ e.status }}"' in LIBRARY_HTML


def test_script_js_sort_library_handles_rewatches():
    match = re.search(r"function sortLibrary\(\)\s*\{.*?\n\}", SCRIPT_JS, re.DOTALL)
    assert match, "sortLibrary() not found in script.js"
    body = match.group(0)
    assert "activeSort === 'rewatches'" in body
    assert "dataset.repeatCount" in body


def test_script_js_apply_library_filters_checks_rewatch_hidden():
    match = re.search(r"function applyLibraryFilters\(\)\s*\{.*?\n\}", SCRIPT_JS, re.DOTALL)
    assert match, "applyLibraryFilters() not found in script.js"
    body = match.group(0)
    assert "rewatchHidden" in body
    assert "rewatchOk" in body


def test_library_html_rewatch_filter_iife_sets_hidden_flag_by_repeat_count():
    assert "dataset.rewatchHidden" in LIBRARY_HTML
    assert "dataset.repeatCount" in LIBRARY_HTML


# ── Behavioral checks (Python re-implementation of the JS semantics) ────────────
# These pin down the exact ordering/filtering behavior the JS above implements —
# most-rewatched first, missing/zero repeat_count sorts last, and the filter is a
# simple "has been rewatched at least once" toggle.

def _sort_by_rewatches(cards):
    """Mirrors sortLibrary()'s activeSort === 'rewatches' branch: descending by
    repeat_count, defaulting missing/falsy values to 0."""
    return sorted(cards, key=lambda c: -(c.get("repeat_count") or 0))


def _rewatch_filter_hides(card, active):
    """Mirrors the rewatch-filter IIFE's applyRewatchFilter() (issue #330): with
    the toggle off, nothing is hidden; with it on, a card stays visible if it has
    a completed rewatch (repeat_count >= 1) OR is a rewatch currently in progress
    (status REPEATING, even with repeat_count still 0 — e.g. a first rewatch not
    yet finished)."""
    if not active:
        return False
    count = card.get("repeat_count") or 0
    is_rewatching = card.get("status") == "REPEATING"
    return count < 1 and not is_rewatching


def test_sort_orders_most_rewatched_first():
    cards = [
        {"id": 1, "repeat_count": 0},
        {"id": 2, "repeat_count": 3},
        {"id": 3, "repeat_count": 1},
    ]
    ordered = [c["id"] for c in _sort_by_rewatches(cards)]
    assert ordered == [2, 3, 1]


def test_sort_treats_missing_repeat_count_as_zero():
    cards = [
        {"id": 1},  # no repeat_count key at all, like a card with data-repeat-count="0"
        {"id": 2, "repeat_count": 2},
    ]
    ordered = [c["id"] for c in _sort_by_rewatches(cards)]
    assert ordered == [2, 1]


def test_filter_off_hides_nothing():
    never_rewatched = {"repeat_count": 0, "status": "WATCHING"}
    rewatched = {"repeat_count": 4, "status": "COMPLETED"}
    assert _rewatch_filter_hides(never_rewatched, False) is False
    assert _rewatch_filter_hides(rewatched, False) is False


def test_filter_on_hides_only_never_rewatched():
    never_rewatched = {"repeat_count": 0, "status": "WATCHING"}
    rewatched_once = {"repeat_count": 1, "status": "COMPLETED"}
    rewatched_many = {"repeat_count": 5, "status": "COMPLETED"}
    assert _rewatch_filter_hides(never_rewatched, True) is True
    assert _rewatch_filter_hides(rewatched_once, True) is False
    assert _rewatch_filter_hides(rewatched_many, True) is False


def test_filter_on_treats_missing_repeat_count_as_never_rewatched():
    assert _rewatch_filter_hides({"status": "WATCHING"}, True) is True


# ── Issue #330: filter also catches an in-progress rewatch ──────────────────


def test_filter_on_shows_a_first_rewatch_still_in_progress():
    """The exact live bug scenario: repeat_count is still 0 (no completed pass
    yet), but status is REPEATING — must count as a rewatch, not be hidden."""
    first_rewatch_in_progress = {"repeat_count": 0, "status": "REPEATING"}
    assert _rewatch_filter_hides(first_rewatch_in_progress, True) is False


def test_filter_on_shows_a_later_rewatch_pass_in_progress_too():
    several_passes_deep = {"repeat_count": 3, "status": "REPEATING"}
    assert _rewatch_filter_hides(several_passes_deep, True) is False


def test_filter_on_still_hides_a_plain_watching_entry():
    assert _rewatch_filter_hides({"repeat_count": 0, "status": "WATCHING"}, True) is True
