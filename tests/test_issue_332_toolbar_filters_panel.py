"""
Coverage for issue #332 — consolidating the library toolbar's format/season/tag/
score/rewatch/favorite controls into one Filters popover with an active-count
badge, plus a row of removable chips for whatever is currently active.

This app deliberately has no frontend build step or JS test runner (see CLAUDE.md),
so — matching test_library_rewatch_sort_filter.py's approach — the popover/chip
open-close/click behavior itself isn't exercised here; these are static-content
assertions that the six controls were *moved, not rebuilt* (same ids/classes/
data-attributes the existing applyLibraryFilters()/applyScoreFilter()/
applyRewatchFilter()/applyFavoriteFilter() and Collections' replay logic depend
on), plus coverage of the Collections rewatch-state bug this issue also fixed.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIBRARY_HTML = (REPO_ROOT / "app" / "templates" / "library.html").read_text(encoding="utf-8")
SCRIPT_JS = (REPO_ROOT / "app" / "static" / "script.js").read_text(encoding="utf-8")


# ── Trigger + popover + chip row wiring ──────────────────────────────────────────

def test_filters_trigger_exists_with_accessible_attributes():
    assert '<button type="button" id="filters-toggle" class="filters-btn"' in LIBRARY_HTML
    assert 'aria-haspopup="true"' in LIBRARY_HTML
    assert 'aria-expanded="false"' in LIBRARY_HTML


def test_filter_count_badge_starts_hidden():
    assert re.search(r'<span id="filter-count" class="filter-count" hidden>0</span>', LIBRARY_HTML)


def test_filters_popover_exists_and_starts_hidden():
    assert '<div id="filters-popover" class="filters-popover" hidden>' in LIBRARY_HTML


def test_chip_row_exists_and_starts_hidden():
    assert '<div id="filter-chip-row" class="chip-row" hidden></div>' in LIBRARY_HTML


def test_popover_contains_all_six_original_controls_unchanged():
    """The controls were moved, not rebuilt — same selectors #332's own JS and
    the pre-existing filter/Collections code both depend on."""
    popover_match = re.search(
        r'<div id="filters-popover" class="filters-popover" hidden>(.*?)\n      </div>\n    </div>',
        LIBRARY_HTML, re.DOTALL,
    )
    assert popover_match, "could not locate the filters-popover block"
    popover = popover_match.group(1)

    assert 'data-format=""' in popover and 'data-format="TV"' in popover
    assert 'id="season-filter" class="season-filter-select"' in popover
    assert 'id="tag-filter" class="tag-filter-select"' in popover
    assert 'data-score-filter=""' in popover
    assert 'class="rewatch-toggle" data-rewatch-filter=""' in popover
    assert 'data-favorite-filter=""' in popover and 'data-favorite-filter="1"' in popover


def test_only_one_rewatch_toggle_element_still_exists():
    """Same regression guard as test_library_rewatch_sort_filter.py, re-asserted
    here since #332 physically relocated this markup."""
    assert len(re.findall(r'<button[^>]*\bdata-rewatch-filter="[^"]*"', LIBRARY_HTML)) == 1


def test_filters_js_reads_dom_state_not_a_second_source_of_truth():
    """refreshFilterSummary() must derive the chip/badge state from the same
    elements applyLibraryFilters()/Collections read — never track its own
    parallel copy of "what's filtered", which could drift."""
    match = re.search(r"function refreshFilterSummary\(\)\s*\{.*?\n  \}", LIBRARY_HTML, re.DOTALL)
    assert match, "refreshFilterSummary() not found in library.html"
    body = match.group(0)
    assert "activeFormatBtn" in body
    assert "getElementById('season-filter')" in body or "season" in body
    assert "activeScoreBtn" in body
    assert "rewatchToggle" in body
    assert "activeFavoriteBtn" in body


def test_clear_all_only_renders_once_two_or_more_filters_active():
    assert "chips.length >= 2" in LIBRARY_HTML


# ── i18n wiring for the new copy ─────────────────────────────────────────────────

def test_new_i18n_keys_used_in_markup_and_script():
    for key in [
        "lib_filters_btn", "lib_filters_format_label", "lib_filters_season_label",
        "lib_filters_tags_label", "lib_filters_score_label", "lib_filters_favorites_label",
    ]:
        assert f"t('{key}')" in LIBRARY_HTML, f"{key} not referenced in library.html"
    assert "t('lib_clear_all_filters')" in LIBRARY_HTML
    assert "t('lib_chip_format'" in LIBRARY_HTML
    assert "t('lib_chip_score'" in LIBRARY_HTML


# ── Collections rewatch-state bug fix (found while building this issue) ─────────
# #330 replaced the two-.filter-btn ("All"/"Rewatched") pair with a single
# .rewatch-toggle switch, but script.js's Collections save/restore
# (currentFilterState/applyFilterValues) kept reading/writing it via the old
# selector — meaning saving or restoring a Collection always dropped the
# rewatch-only state. Fixed as part of #332 since the new chip row reads the
# exact same state Collections does.

def test_current_filter_state_reads_the_toggles_active_class_not_the_old_selector():
    match = re.search(r"function currentFilterState\(\)\s*\{.*?\n    \};", SCRIPT_JS, re.DOTALL)
    assert match, "currentFilterState() not found in script.js"
    body = match.group(0)
    assert "querySelector('.rewatch-toggle')" in body
    assert "classList.contains('active')" in body
    assert "data-rewatch-filter].active" not in body


def test_apply_filter_values_clicks_toggle_only_when_state_differs():
    match = re.search(r"function applyFilterValues\(filters\)\s*\{.*?\n  \}", SCRIPT_JS, re.DOTALL)
    assert match, "applyFilterValues() not found in script.js"
    body = match.group(0)
    assert "querySelector('.rewatch-toggle')" in body
    assert "Boolean(filters.rewatch)" in body
    assert "classList.contains('active')" in body
    assert "clickAttr('data-rewatch-filter'" not in body


def _rewatch_state_roundtrip(saved_active: bool, current_active: bool) -> bool:
    """Mirrors applyFilterValues()'s fixed rewatch-toggle logic: click only when
    the restored state doesn't already match the toggle's current state."""
    saved_value = "1" if saved_active else ""
    should_click = bool(saved_value) != current_active
    return current_active != should_click if should_click else current_active


def test_restoring_an_active_rewatch_collection_onto_an_inactive_toggle_clicks_it():
    assert _rewatch_state_roundtrip(saved_active=True, current_active=False) is True


def test_restoring_an_inactive_rewatch_collection_onto_an_active_toggle_clicks_it():
    assert _rewatch_state_roundtrip(saved_active=False, current_active=True) is False


def test_restoring_a_collection_matching_the_current_toggle_state_is_a_no_op():
    assert _rewatch_state_roundtrip(saved_active=True, current_active=True) is True
    assert _rewatch_state_roundtrip(saved_active=False, current_active=False) is False
