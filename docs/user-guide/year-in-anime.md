# Year in anime (Wrapped)

`/stats/wrapped` — a living "your year so far" page for the current calendar year,
reachable any time from the header's Wrapped icon (next to Settings) rather than a
permanent nav tab, since it's a periodic recap you check in on rather than a page you
browse daily. There's no year picker here — it always reflects wherever the current
year is today. For a specific past year's wrap-up, see the `#wrapup-card` on
[Stats](stats.md) instead, which is a separate reveal with its own year selector.

## The animated slide flow

The first time you open the page in a given year, it auto-launches a full-screen,
Spotify-Wrapped-style flow: ten highlights, one per screen — cold open, episode count,
watch time, pace vs. last year, top genre, biggest binge week, most rewatched, highest
rated (a hero reveal), how your score distribution shifted, and a closing recap card.
Tap, click, use the arrow keys, or swipe to move between slides; each auto-advances on
its own after a few seconds unless you tap and hold to pause. It only auto-launches once
per year — after that, a **Play** button re-opens it on demand. If your device has
reduced-motion turned on, auto-advance and transitions are skipped in favor of plain
manual navigation.

The closing recap card has a **Save image** button that renders a real PNG of your
recap (episodes, watch time, top genre, highest rated) and downloads it straight to
your device — there's no public link or share URL, it's a local image file only.

## The static detail view

Everything the animated flow shows is also rendered as an ordinary, always-visible page
underneath it — useful for re-reading a specific stat without replaying the whole
sequence, and as a no-JS/accessibility fallback. Same data, same source
(`_compute_wrapped_page()`), just laid out as a normal page instead of full-screen
slides.
