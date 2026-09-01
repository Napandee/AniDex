# Personal notes

AniList tracks status, progress, and score. Everything else about how you actually
relate to a show — why you dropped it, what you thought, how badly you want to get to
it next — has no home in AniList's own data model. That's what this layer is for, and
it's the actual reason AniDex exists.

Open any anime's detail page to edit these fields:

- **Drop reason** — freeform text, shown next to DROPPED entries so future-you knows
  *why*, not just *that*.
- **Custom tags** — your own labels, independent of AniList's genre/tag system. Used by
  [Collections](collections.md) and bulk-tagging. Manage every tag you've ever created
  — rename, merge two into one, or delete — from the **Tags** page (linked from
  Settings), rather than editing each anime's notes individually.
- **Mood tags** — a fixed picklist (comfort, hype, intense, sad, wholesome, dark, funny,
  relaxing, thought_provoking, bittersweet) for filtering by "what kind of watch is
  this," separate from freeform tags.
- **Notes** — freeform text for anything else.
- **Watch-next priority** — a manual number that factors into the [queue](queue-and-upcoming.md)'s
  ordering alongside the recommendation score.
- **Favorite** — a heart flag, independent of your numeric score.

## Rewatch notes

Each rewatch gets its **own** note history, separate from your first-watch notes — so
"what I thought the first time" and "what I thought on rewatch #2" don't overwrite each
other. The repeat count itself is tracked too, and feeds into [Stats](stats.md)'s
most-rewatched breakdown and the Watch Queue's rewatch-reminder section.

## Related titles / watch order

If a title has prequels, sequels, or other AniList-defined relations, its notes page
lists them in watch order along with your real status and progress on each one you've
tracked — not just whether it's in your library, but where you actually stand on it.
Useful for picking up a franchise partway through without hunting down what's next.

## None of this touches AniList

Drop reasons, tags, notes, and priority are AniDex-only. Sync jobs never write to this
layer, and it's never pushed to AniList — only status, progress, and score round-trip
there.
