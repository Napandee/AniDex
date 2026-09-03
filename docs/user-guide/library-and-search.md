# Library and search

## Library view

Your full AniList list, one card per anime: cover art, title, star rating, episode
progress, favorite flag, format, and the status/airing/stale/rewatch/tag badges always
visible on the card. Filter by format, season, tag, and score. Click anywhere on a card
to open its notes/detail view. For a quick edit without leaving the list, use the
star-rating widget to set your score or the episode stepper (−/+) to bump progress —
both update in place on the card.

**More details toggle** — genres, AniList's average score, streaming links, and a
preview of your drop reason/notes are one click away behind a "more details" (⋯) toggle
on each card rather than always shown — the same collapse mechanism used for the
[manga/light novel](manga-tracking.md) badge. This just hides them from view; search
still matches genre text either way.

**Bulk edit** — select multiple entries to change status or apply tags to all of them
at once. Status changes go through the same outbox queue as everything else that talks
to AniList (see [Sync providers](sync-providers.md)), so a large batch won't get
rate-limited or lost if AniList is briefly slow to respond.

**Streaming links** are community-curated on AniList's side and can lag real
availability — a "last synced" timestamp sits next to them so you know how stale they
might be, rather than presenting them as guaranteed-current. (They're inside the "more
details" toggle above, not shown by default.)

**Empty library vs. an empty filter result** — if your library genuinely has nothing in
it yet, the page shows a CTA button straight into adding your first anime. If it's the
current filters that leave nothing matching, you get a **Clear filters** button instead,
which resets every active filter (format, score, favorite, rewatch, season, tag) and
re-runs the search — these are two different situations and no longer look identical.

## Search

Two different things live under "search," deliberately kept separate:

- **In-library search** — searches only what's already in your list. This is what you
  want for "where's that show I already added." If a search turns up nothing, the
  zero-results page offers a button straight into quick-add (below), pre-filled with
  your search term, instead of just dead-ending.
- **Quick-add** — looks up a title on AniList by name and adds it straight to your
  list. This is a lookup, not a catalog browser — AniDex doesn't try to replicate
  AniList's own browse/discover UI. For that, use AniList directly; AniDex links out to
  it wherever relevant.

## Export / Import

Settings has an export that downloads your entire library plus every bit of the
personal layer (notes, tags, drop reasons, collections) as a single file. The same file
restores via import — useful for moving to a fresh instance, or just keeping an offline
backup of the data AniList itself doesn't store.
