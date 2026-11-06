# Setting up sync providers

Setup for each provider. How sync behaves once it's on: [Sync providers](../user-guide/sync-providers.md).

## Crunchyroll sync (optional)

If you watch on Crunchyroll, the app can pull your watch history and push progress
updates to AniList — this runs inside the main app container as part of the same daily
sync, no separate container needed.

> **Note:** this reads your Crunchyroll session cookie directly against Crunchyroll's own
> (unofficial, undocumented) API — it isn't affiliated with or supported by Crunchyroll,
> and could stop working if Crunchyroll changes their site. Using any tool that acts on
> your behalf with your session credentials is ultimately your own call with respect to
> Crunchyroll's Terms of Service. This feature is entirely optional — skip it if you'd
> rather not take that on.

To get your Crunchyroll session cookie: log into crunchyroll.com in a browser, open
DevTools → Application → Cookies → `https://www.crunchyroll.com`, and copy the value of
the `etp_rt` cookie. Set it as `CRUNCHYROLL_ETP_RT` in your `.env` (or in Settings after
first launch).

Once set, it runs automatically as part of the daily sync — or trigger it anytime via
the "Sync Now" button on the Settings page (or `POST /api/sync`).

See the [sync providers guide](../user-guide/sync-providers.md) for what happens to
a title that isn't already in your library yet.

## Netflix sync (optional)

If you watch on Netflix, the app can pull your viewing activity and push progress
updates to AniList — same pattern as Crunchyroll sync above, runs inside the main app
container as part of the same daily sync.

> **Note:** this reads your Netflix session cookies directly against Netflix's own
> (unofficial, undocumented) "Falcor" API — it isn't affiliated with or supported by
> Netflix, and could stop working if Netflix changes their site. Same ToS caveat as
> Crunchyroll sync above: using any tool that acts on your behalf with your session
> credentials is ultimately your own call. This feature is entirely optional — skip it
> if you'd rather not take that on.
>
> Netflix's viewing-activity feed has no absolute episode-ordinal field, so progress is
> tracked by counting distinct new episodes since the last sync and adding that to
> AniList's current progress, rather than setting an absolute number the way Crunchyroll
> sync does. This is correct for watching in order, but can overcount if you watch out of
> order or skip around — same as any sync mismatch in this app, it's a one-click manual
> fix and never touches your score or notes.

To get your Netflix credentials: log into netflix.com in a browser, open DevTools →
Network, find any request to a `netflix.com` API endpoint, and copy the full `cookie`
request header value (not just the `NetflixId`/`SecureNetflixId` cookies — the
viewing-activity API checks several others too) as `NETFLIX_COOKIE_HEADER`. Then find the
`guid` value in that same request's body/params and set it as `NETFLIX_PROFILE_GUID`.
Set both in your `.env` (or in Settings after first launch). `scripts/dev/setup-netflix-env.sh`
is an interactive helper for (re-)populating these when developing locally.

Once set, it runs automatically as part of the daily sync — or trigger it anytime via
the "Sync Now" button on the Settings page (or `POST /api/sync`). A one-time CSV import
is also available as a bootstrap alternative for accounts with a lot of history — see
the [sync providers guide](../user-guide/sync-providers.md).

## Plex sync (optional)

If you watch on Plex, the app can pull your watch history from your own Plex Media
Server and push progress updates to AniList. Unlike Crunchyroll/Netflix/Prime Video
above, this uses Plex's own real, documented OAuth sign-in flow — no cookie to find
or paste. From Settings' Plex card, click **Connect**, approve access in the plex.tv
tab that opens, and pick which of your Plex servers to sync from. AniDex stores a
server-scoped token from that flow, the same PIN-based approach apps like Overseerr
and Tautulli use.

Progress is set to an absolute episode number from Plex's own watch history, so it's
accurate even out-of-order viewing. If your library uses an anime-specific metadata
agent (HAMA or MyAnimeList.bundle), AniDex checks Plex's own AniDB/MAL id first as a
strictly better match signal before falling back to title matching.

Once connected, it runs automatically as part of the daily sync — or trigger it
anytime via the "Sync Now" button on the Settings page (or `POST /api/sync`).

## Prime Video sync (optional)

If you watch on Prime Video, the app can pull your watch history and push progress
updates to AniList — same cookie-based pattern as Netflix above, runs inside the main
app container as part of the same daily sync.

> **Note:** this reads your Prime Video session cookie directly against Amazon's own
> (unofficial, undocumented) API — it isn't affiliated with or supported by Amazon,
> and could stop working if Amazon changes their site. Same ToS caveat as Crunchyroll/
> Netflix sync above. This feature is entirely optional — skip it if you'd rather not
> take that on.
>
> Amazon's watch-history page sits behind a shorter-lived session tier than general
> Prime Video browsing, so this cookie tends to need refreshing more often than
> Crunchyroll's or Netflix's — expect to occasionally repeat the capture steps below
> even while still logged into primevideo.com everywhere else.

To get your Prime Video credentials: log into primevideo.com in a browser, go to
**Settings → Watch History**, open DevTools → Network, click any
`getWatchHistorySettingsPage` request, and copy the whole `cookie` request header
value under Request Headers as `PRIMEVIDEO_COOKIE_HEADER`. Set it in your `.env` (or
in Settings after first launch).

Once set, it runs automatically as part of the daily sync — or trigger it anytime via
the "Sync Now" button on the Settings page (or `POST /api/sync`).
