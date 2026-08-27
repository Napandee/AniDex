# Library and search

## Library view

Your full AniList list, one card per anime: cover art, star rating, status, episode
progress, and (where AniList has them) streaming links. Filter by format, season, tag,
and score. Click anywhere on a card to open its notes/detail view. For a quick edit
without leaving the list, use the star-rating widget to set your score or the episode
stepper (−/+) to bump progress — both update in place on the card.

**Bulk edit** — select multiple entries to change status or apply tags to all of them
at once. Status changes go through the same outbox queue as everything else that talks
to AniList (see [Sync providers](sync-providers.md)), so a large batch won't get
rate-limited or lost if AniList is briefly slow to respond.

**Streaming links** are community-curated on AniList's side and can lag real
availability — a "last synced" timestamp sits next to them so you know how stale they
might be, rather than presenting them as guaranteed-current.

## Search

Two different things live under "search," deliberately kept separate:

- **In-library search** — searches only what's already in your list. This is what you
  want for "where's that show I already added."
- **Quick-add** — looks up a title on AniList by name and adds it straight to your
  list. This is a lookup, not a catalog browser — AniDex doesn't try to replicate
  AniList's own browse/discover UI. For that, use AniList directly; AniDex links out to
  it wherever relevant.

## Export / Import

Settings has an export that downloads your entire library plus every bit of the
personal layer (notes, tags, drop reasons, collections) as a single file. The same file
restores via import — useful for moving to a fresh instance, or just keeping an offline
backup of the data AniList itself doesn't store.
