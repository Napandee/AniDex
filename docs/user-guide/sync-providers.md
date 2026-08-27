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

Same idea as Crunchyroll/Netflix — a cookie-authenticated live sync against Prime
Video's own watch-history endpoint, set up the same way (grab the cookie header from
DevTools, save it in Settings). One real caveat worth knowing up front: Amazon's
session for this specific endpoint expires noticeably faster than general
browsing/playback does, by design on Amazon's side — expect to need to refresh the
cookie more often than Crunchyroll or Netflix's.

**CSV import** (fallback for when the live sync's cookie keeps going stale) — request
your Prime Video viewing history export from Amazon (Account → Privacy → "Request My
Data" → `Digital.PrimeVideo.ViewingHistory`) and upload the resulting file in Settings.
Unlike Netflix's CSV import, this one only updates titles already in your AniList
library — it never creates new entries on its own, so it's always safe to re-import.
Note: Amazon doesn't offer a direct one-click "download my history" button for Prime
Video the way Netflix does — the "Request My Data" request can take a while to
fulfill.

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
