# Recommendations

The `/recommendations` page scores your unwatched and planning-list anime against the
genres, tags, and studios of your highest-rated completed shows, and surfaces the
best matches.

## How scoring works

Each candidate's score is a weighted blend:

| Signal | Weight |
|---|---|
| Genre overlap | 40% |
| Tag overlap | 25% |
| Studio overlap | 15% |
| Cross-user signal (other users' ratings, if enabled) | 20% |

The cross-user signal is opt-in — see [Multi-user](multi-user.md)'s "also watching"
section. On a single-user instance, or if you haven't opted in, that 20% simply
contributes nothing — every candidate scores 0 on it, so it doesn't change how
candidates rank against each other (there's no redistribution of the weight onto
the other three signals; the score is just capped lower before the final
0–100 normalization).

Candidates come from two sources: AniList's own per-show "recommendations" edge for
your top-rated completed shows, and your own Planning list (scored on the same scale,
so backlog re-prioritization and genuine discovery sit side by side).

## Acting on a recommendation

- **Dismiss** with a reason (not interested / already watched / wrong genre / too long)
  — removes it and records why, so patterns in what you reject are visible later.
- **Snooze** — hides it for 30 days, then it resurfaces automatically. No custom
  duration picker yet; 30 days is currently fixed.
- **Mark as seen** — the fastest path for "I've actually watched this, just never
  logged it here": pushes COMPLETED status and your rating to AniList in one step,
  instead of dismissing it and then separately going to add it properly.

## Seasonal discovery

A "new this season" digest surfaces currently-airing anime that match your taste
profile, alongside the main similarity-based recommendations — useful for catching new
releases the per-show "recommendations" edge wouldn't otherwise show you, since that
edge is anchored to shows you've already completed.

## A note on where this is headed

Recommendation quality is measured, not assumed — AniDex tracks whether a
recommendation actually turns into something you add or rate well (a "hit rate"), and
future scoring changes get validated against that real data before shipping, rather
than tuned on vibes.
