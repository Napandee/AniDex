# Manga / light novel tracking

For anime adapted from a manga or light novel, AniDex can show whether the source
material is still being published — useful for knowing if there's more story to read
beyond where the anime left off, without leaving the library page to go check yourself.

## Where it shows up

A small 📖 icon sits in each library card's footer, alongside the AniList link and
remove buttons, but only for anime that actually have a tracked source (an original,
non-adaptation anime like *Cowboy Bebop* gets no icon at all). Clicking it expands a
detail panel — the same disclosure mechanism used elsewhere in AniDex (see [Library and
search](library-and-search.md)'s "more details" toggle) — showing, per source:

- **Type** — Manga or Light Novel
- **Status** — Ongoing, Completed, On hiatus, or Cancelled
- **Latest chapter or volume** — whichever the source data provides
- **English licensor**, if known

Some anime have more than one source worth showing (e.g. a manga adaptation alongside
the original light novel) — the panel lists each one as its own row.

## Where the data comes from

This is background sync data, not something you configure — populated weekly by a
catalog-wide job (not per-user), matched against AniList's own relations first, then
cross-referenced against MangaDex and MangaUpdates only when their listing can be tied
back to the exact same AniList id (never a fuzzy title match). When a confident match
isn't available, the badge falls back to whatever AniList itself already knows (status
and a licensor pulled from its external links) rather than showing nothing.

Because MangaDex only hosts manga (not a light novel's own prose), a light-novel-sourced
title is more likely to fall back to this AniList-only data than a manga-sourced one —
that's a known gap in the underlying data, not a bug.
