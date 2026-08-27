# Sync providers

AniList is the source of truth this app reads from, but if you actually *watch* on
Crunchyroll or Netflix rather than tracking manually, AniDex can read your watch
history from those services directly and push progress updates to AniList for you.
Both are entirely optional, run as part of the same daily sync (no separate
container), and can be triggered on demand from Settings' "Sync Now" button.

> **A note on how these work:** both read your session cookie directly against the
> service's own unofficial, undocumented API — neither is affiliated with or
> supported by Crunchyroll/Netflix, and either could stop working if those sites
> change. Whether that's an acceptable trade-off for your own account is your call.

## Crunchyroll

Log into crunchyroll.com in a browser, open DevTools → Application → Cookies →
`https://www.crunchyroll.com`, and copy the value of the `etp_rt` cookie. Set it as
`CRUNCHYROLL_ETP_RT` in Settings (or `.env` before first launch).

Progress is set to an absolute episode number from Crunchyroll's own max-aggregated
history, so it's accurate even if you've watched out of order.

## Netflix

Log into netflix.com, open DevTools → Network, find any request to a netflix.com API
endpoint, and copy the **full** `cookie` request header value (not just
`NetflixId`/`SecureNetflixId` — the viewing-activity API checks several others too) as
`NETFLIX_COOKIE_HEADER`. Then find the `guid` value in that same request and set it as
`NETFLIX_PROFILE_GUID`. Both go in Settings (or `.env`).

Netflix's own activity feed has no absolute episode number, only a list of watched
titles/episodes — so progress is tracked by counting *distinct new episodes* since the
last sync and adding that to AniList's current progress, rather than setting an
absolute number the way Crunchyroll sync does. This is correct for watching in order,
but can overcount if you skip around. Like any sync mismatch in this app, it's a
one-click manual fix and never touches your score or notes.

**CSV import** — if you have a lot of existing Netflix history, a one-time CSV import
(from Netflix's own "download your viewing history" export) is available as a bootstrap
alternative to walking your full history through the API on first connect.

## Prime Video

Log into primevideo.com, open DevTools → Network, find a request to
`primevideo.com/api/getWatchHistorySettingsPage`, and copy the full `cookie`
request header value as `PRIMEVIDEO_COOKIE_HEADER` in Settings (or `.env`).

Progress is tracked as an absolute episode number, same as Crunchyroll's approach —
accurate even watching out of order.

**A note specific to Prime Video, unlike Crunchyroll/Netflix:** Amazon's session for
the watch-history endpoint expires much faster than ordinary browsing/playback stays
logged in, so a manually-captured cookie tends to go stale within a day or so — this
isn't a bug in AniDex, it's how Amazon's own session model works (every other
community tool that reads this same endpoint hits the same limit). Two ways to make
this less painful than re-capturing the cookie by hand every time it dies:

- **[Prime Video Cookie Sync browser extension](../../browser-extension/primevideo-cookie-sync/)**
  (issue #390) — a small companion extension that reads your browser's current
  Prime Video cookies and pushes them to your AniDex instance automatically (hourly,
  or on demand). Recommended if the manual recapture is happening often enough to be
  annoying.
- **CSV import** (issue #389) — a one-time export of your Prime Video watch history
  from Amazon's own account settings, imported the same way Netflix's CSV import
  works above. A good fallback for whenever the live cookie sync is stale between
  refreshes, rather than something to redo constantly.

## What happens with a title you haven't added yet

If a synced title matches something already on your AniList list, progress just
updates normally. If it doesn't match anything yet:

- On your **very first sync** (or a **Force Full Resync**, Settings → Sync → "Force
  full resync"), AniDex is conservative and won't auto-create new entries — walking
  your entire watch history for the first time and creating dozens of new AniList
  entries all at once would flood your real list.
- On a normal **day-to-day incremental sync**, a genuinely new title *will* be added
  automatically — at WATCHING status for a series, or COMPLETED for a movie (a movie
  has no sensible "still watching" resting state).

Either way, ratings, drop reasons, and notes are never touched by any sync — only
status and progress.
