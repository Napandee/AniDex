# Sync providers

AniList is the source of truth this app reads from, but if you actually *watch* on
Crunchyroll, Netflix, Plex, or Prime Video rather than tracking manually, AniDex can
read your watch history from those services directly and push progress updates to
AniList for you. All four are entirely optional, run as part of the same daily sync
(no separate container), and can be triggered on demand from Settings' "Sync Now"
button.

> **A note on how these work:** Crunchyroll, Netflix, and Prime Video all read your
> session cookie directly against the service's own unofficial, undocumented API —
> none is affiliated with or supported by that service, and any could stop working
> if that site changes. Plex is the exception — it uses Plex's own real, documented
> OAuth sign-in flow against your own Plex Media Server, not a cookie paste.
> Whether the cookie-based trade-off is acceptable for your own account is your
> call.

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

## Plex

Unlike the three services above, Plex uses a real, documented OAuth sign-in flow — no
cookie to find or paste. From Settings' Plex card, click **Connect** — a new tab opens
to plex.tv asking you to approve access, then AniDex lists the Plex Media Servers your
account can see so you can pick which one to sync from. AniDex stores a
server-scoped token from that flow (not your plex.tv account password or a
long-lived account token), matching the same PIN-based flow apps like Overseerr and
Tautulli use for "Sign in with Plex".

Progress is set to an absolute episode number from Plex's own watch history
(`parentIndex`/`index` on each episode, the same real season/episode metadata
Crunchyroll's sync reads), so it's accurate even if you've watched out of order. If
your Plex library uses an anime-specific metadata agent (HAMA or MyAnimeList.bundle),
AniDex checks Plex's own AniDB/MAL id field first — a strictly better match signal —
before falling back to matching by title the way the other three providers do.

## Prime Video

Log into primevideo.com in a browser, go to **Settings → Watch History**, open
DevTools → Network, click any `getWatchHistorySettingsPage` request, and copy the
whole `cookie` request header value under Request Headers. Set it as
`PRIMEVIDEO_COOKIE_HEADER` in Settings (or `.env` before first launch) — same
cookie-paste pattern as Netflix.

Progress is set to an absolute episode number the same way Crunchyroll's and Plex's
sync do, since Prime Video's watch-history API reports an exact "Episode N: &lt;title&gt;"
per watched episode, not just a raw count.

> **A note on session lifetime:** Prime Video's cookie tends to need refreshing more
> often than Crunchyroll's or Netflix's — Amazon's watch-history page sits behind a
> shorter-lived session tier than general browsing, so expect to occasionally repeat
> the capture steps above even if you're still logged into primevideo.com everywhere
> else. If your Prime Video sync starts failing, recapturing a fresh cookie the same
> way is the first thing to try. This isn't a bug in AniDex — every other community
> tool that reads this same Amazon endpoint hits the same limit. Two ways to make
> this less painful than re-capturing the cookie by hand every time it dies:
>
> - **[Prime Video Cookie Sync browser extension](../../browser-extension/primevideo-cookie-sync/)**
>   (issue #390) — a small companion extension that reads your browser's current
>   Prime Video cookies and pushes them to your AniDex instance automatically
>   (hourly, or on demand). Recommended if the manual recapture is happening often
>   enough to be annoying.
> - **CSV import** (issue #389) — a one-time export of your Prime Video watch
>   history from Amazon's own account settings, imported the same way Netflix's CSV
>   import works above. A good fallback for whenever the live cookie sync is stale
>   between refreshes, rather than something to redo constantly.

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
