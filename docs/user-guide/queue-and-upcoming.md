# Queue and upcoming

## Watch Queue

`/queue` has four tabs: **All**, **Planning**, **Paused**, and **Watching**.

All/Planning/Paused is your watch-*next* list — Planning and Paused entries, ordered by
a blend of recommendation score and your own manual
[watch-next priority](personal-notes.md), and reorderable by drag. Filter by tag or by
an episode-count bucket (useful for "give me something short" vs. "I have a free
weekend").

**Watching** answers a different question — "what am I mid-watch on right now," not
"what should I start next." It lists everything currently in Watching status, most
recently progressed first, so picking up where you left off doesn't mean falling back to
the full Library view and filtering by hand. It doesn't use the
recommendation-score/watch-next-priority ordering the other tabs do (that's a
prioritization signal for what to *start*, not a recency signal for something already in
progress), and entries here aren't drag-reorderable, since there's no queue position to
set for something you're already watching.

A **rewatch reminder** section sits alongside the main queue, surfacing shows you've
marked for rewatch or that your rewatch notes suggest you might be due to revisit.

If a filter leaves a tab with nothing to show, a **Clear filters** button resets every
active filter on the page rather than just describing the empty state.

## Upcoming

`/upcoming` shows the airing schedule for anything in your Watching or Planning list —
what's airing today, this week, and a weekly Monday–Sunday broadcast grid view for
planning your week around new episodes.

**On this day** — the default `/upcoming` view also shows watch-start and
watch-finish anniversaries landing on today's calendar date in a prior year (e.g. "You
started this 2 years ago today"). It's computed on the fly from your existing
start/finish dates, so there's nothing to set up — it just appears when there's a
match, and only on the plain default view (not when you've navigated to a different
week or filtered to a specific day).
