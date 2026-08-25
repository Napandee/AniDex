# Streaming coverage

`/streaming` answers a specific question: **"if I added service X, how many more
episodes of what I'm already watching or planning would that unlock?"**

## Setting it up

In Settings, tell AniDex which streaming services you already have ("services I own").
That's the only input this feature needs — everything else is computed from your
existing Watching/Planning library entries and AniList's own streaming-link data.

## How it's scored

Rather than a raw "80% of your list is on Crunchyroll" percentage, AniDex ranks
services by **marginal value**: how many additional episodes-remaining across your
Watching/Planning shows would newly become covered if you added that service. A
service that would unlock 40 episodes you're actively partway through ranks above one
that would only unlock 5, even if the second service technically has more titles in
your library overall.

Only Watching/Planning entries drive the ranking — completed and dropped shows don't
affect it, since the question this page answers is about what you'd unlock *next*, not
a historical coverage percentage.

A compact summary card on [Stats](stats.md) links through to the full page.

## What this isn't (yet)

Region-aware availability, a "cancel candidates" framing (which service could you drop
with the least loss), a household-aggregate view, and set-cover-style optimization
were all considered and deliberately deferred — this is v1's simpler marginal-value
framing, not the ceiling of what's planned.
