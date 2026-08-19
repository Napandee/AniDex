# Plex sync — research spike findings

Tracking issue: #151, a research spike blocking the implementation issue #153. Parent
epic: #148 (Plex/Jellyfin integration; Jellyfin covered separately, not touched here).

**Scope correction from the issue title:** the issue title says "verify... against a
live instance," but unlike Prime Video (#17, which genuinely needed live traffic
capture because no OSS client existed with real endpoint knowledge), Plex has no live
instance available right now, and — more importantly — doesn't need one to make the
three scope decisions. `python-plexapi` (https://github.com/pkkid/python-plexapi) is a
mature, actively-maintained, MIT-licensed library that already implements auth, history
retrieval, and metadata access against Plex's real (undocumented-officially but
extensively reverse-engineered) API. This pass reads its actual source — not just its
README — the same "read the real implementation, not the description" method the
Prime Video follow-up pass used successfully in #15's research. Every claim below is
tagged with how it was confirmed.

**Status: all three technical questions have a source-code-confirmed answer with high
confidence. Nothing here required a live server.** The gap that remains is integration
testing (does this app's own code, once written, actually work against a real Plex
account) — that's normal pre-merge verification for issue #153, not a blocker for this
spike's acceptance criteria. See "What's still open" below for exactly what that
means and doesn't mean.

## 1. Auth model

Plex has two distinct token concepts, both implemented in `python-plexapi`, and they
are not interchangeable:

- **plex.tv account token** (`MyPlexAccount`) — proves who the *Plex account* is, not
  which server. Obtained either by posting credentials directly to
  `https://plex.tv/api/v2/users/signin` (`MyPlexAccount.__init__`/`_signin` in
  `myplex.py`, confirmed by reading the method body — builds a `{'login', 'password',
  'rememberMe'}` payload, POSTs it, reads `authToken` off the XML response), or via the
  **OAuth PIN flow** (`MyPlexPinLogin` in the same file): the app requests a PIN from
  `https://plex.tv/api/v2/pins`, builds an `oauthUrl()` the user opens in their own
  browser to complete a real plex.tv sign-in (2FA included) without ever handing their
  password to this app, and the app polls the same PIN endpoint (`_pollLogin`,
  1-second interval, configurable timeout) until `authToken` appears. This is the exact
  flow every third-party Plex app (Overseerr, Tautulli, Ombi, etc.) uses for "Sign in
  with Plex" — confirmed from the class's own docstring/example:
  ```python
  pinlogin = MyPlexPinLogin(oauth=True)
  pinlogin.run()
  print(f'Login to Plex at the following url:\n{pinlogin.oauthUrl()}')
  pinlogin.waitForLogin()
  token = pinlogin.token
  account = MyPlexAccount(token=token)
  ```
- **Server-scoped token** (`X-Plex-Token`, used by `PlexServer`) — the token an actual
  Plex Media Server instance accepts on every request (`PlexServer._headers()` sets
  `X-Plex-Token` on every call). This is *not* the same token as the account token
  above and is **not** something the user has to go find and copy by hand: once the
  app holds an account-level `authToken`, `MyPlexAccount.resources()` calls
  `https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1&includeIPv6=1` and
  returns one `MyPlexResource` per server the account can see, **each carrying its own
  `accessToken`** (confirmed reading `MyPlexResource._loadData`:
  `self.accessToken = data.attrib.get('accessToken')`). `MyPlexResource.connect()`
  then picks the best reachable connection (local → remote → relay,
  `DEFAULT_LOCATION_ORDER`) and builds a `PlexServer` using that resource's own
  `accessToken` — the account token is exchanged for the server token automatically,
  never surfaced to the user.

**Recommendation: OAuth PIN flow, not a raw-token paste box.** This is a real
recommendation, not a hedge — the resource-discovery mechanism above is exactly what
removes the "ask a user to paste a raw server token" awkwardness the issue's open
question flags. Concretely, for #153's implementation:

- The user clicks "Connect Plex" in Settings, gets redirected to the `oauthUrl()`,
  signs in on plex.tv (own password, own 2FA, never touches this app), and the app
  polls until it has an account `authToken`.
- That token calls `resources()` once to list servers, and the user picks (or the app
  auto-selects, if only one) the target server — the app stores the resource's own
  `accessToken` (server-scoped) plus the chosen connection URL, not the account token.
- This maps onto this app's **existing OAuth pattern**, not the Crunchyroll/Netflix
  cookie-paste pattern. Confirmed from `app/main.py`: Google/Discord already use
  `/settings/link/{provider}` + a dedicated `/auth/link-callback/{provider}` route kept
  separate from ordinary login (per `CLAUDE.md`'s Decisions Made section), which is
  structurally the same shape a Plex PIN-based connect flow would need (redirect out,
  come back with a token) — closer to that than to the single password-type input used
  for `cr_etp_rt` / `netflix_cookie_header` in `app/templates/settings.html`. Plex
  simply doesn't have the "grab this cookie from your browser" pattern CR/Netflix are
  stuck with — it has a real, if non-standard, OAuth-shaped flow, so there's no reason
  to fall back to the more awkward paste-box pattern out of habit.
- Practical implementation detail worth flagging for #153: the PIN flow as shown in
  `python-plexapi` is a client-side "open this URL, poll in a background thread" flow
  (built for desktop/CLI apps), not a server-side HTTP redirect+callback like
  Google/Discord OAuth. #153 will need to adapt it slightly — e.g. an endpoint that
  creates the PIN server-side and hands the frontend the `oauthUrl()` plus a PIN id,
  then a small polling endpoint or background task the Settings page hits until the
  token lands — rather than copying `MyPlexPinLogin`'s thread-based polling verbatim
  into a request-handling process. This is an implementation detail for #153, not a
  reason to doubt the flow's viability.
- Storing the account token (not just the derived server token) is worth keeping too,
  since re-deriving the server token later (e.g. server IP changes) just means calling
  `resources()` again — no new user interaction needed, unlike the CR/Netflix cookie
  pattern where an expired cookie means asking the user to go find a new one.

**Confidence: high, from source code.** Not live-tested. The mechanics (endpoint URLs,
payload shapes, token exchange) are read directly from `python-plexapi`'s actual
implementation, which is exercised continuously by a large real-world user base
(Overseerr/Tautulli/etc. depend on the same flows) — this isn't guesswork about an
undocumented API, it's reading working code. What's *not* confirmed live: whether this
app's own eventual PIN-creation/polling code, once written, actually completes an
end-to-end sign-in against a real account (ordinary pre-merge verification, not an
open research question).

## 2. Watch-history / watched-status endpoint shape

Two related but distinct things `python-plexapi` exposes, both read from source:

**History (what was watched, when):**

`PlexServer.history()` — full body confirmed by reading `server.py` directly:

```python
def history(self, maxresults=None, mindate=None, ratingKey=None, accountID=None, librarySectionID=None):
    args = {'sort': 'viewedAt:desc'}
    if ratingKey:
        args['metadataItemID'] = ratingKey
    if accountID:
        args['accountID'] = accountID
    if librarySectionID:
        args['librarySectionID'] = librarySectionID
    if mindate:
        args['viewedAt>'] = int(mindate.timestamp())
    key = f'/status/sessions/history/all{utils.joinArgs(args)}'
    return self.fetchItems(key, maxresults=maxresults)
```

This is a **GET to `/status/sessions/history/all`** on the Plex server itself (not
plex.tv), authenticated via `X-Plex-Token`, already **newest-first
(`sort=viewedAt:desc`)** and already supporting a **`viewedAt>` minimum-timestamp
filter** — i.e. Plex's own API natively supports exactly the "paginated, newest-first,
stop at a watermark" incremental-fetch shape this app already uses for Crunchyroll/
Netflix (`cr_sync_state.last_seen_watched_at` / `netflix_sync_state.last_seen_watched_at`
in `schema.sql`) — no client-side pagination workaround needed the way Prime Video's
history page required. `MyPlexAccount.history()` and the per-share variant on
`MyPlexUser`/`MyPlexServerShare` wrap the same shape at the account level (across all
owned/shared servers) rather than one server.

Response shape, corroborated two ways — reading `PlexPartialObject`/`fetchItems`'s XML
parsing in `python-plexapi`, and Plexopedia's independent write-up of the same
endpoint (https://www.plexopedia.com/plex-media-server/api/server/session-history/,
https://www.plexopedia.com/blog/get-session-history/) — is a `MediaContainer` wrapping
one `<Video>` element per watch event, with attributes including `accountID`,
`deviceID`, `historyKey`, `key`, `librarySectionID`, `ratingKey`,
`originallyAvailableAt`, `thumb`, `title`, `type`, and `viewedAt` (unix timestamp).
`type` distinguishes `episode`/`movie`/etc.; for episodes the parent-series title comes
through the usual `grandparentTitle` field python-plexapi maps on its `Episode` class.

**Watched status on a library item (as opposed to a history log entry):**

`plexapi/mixins/played_unplayed.py`'s `PlayedUnplayedMixin` (mixed into `Video`,
`Movie`, `Episode`, etc. per `video.py`) — confirmed reading the file directly:

- `isPlayed` → `bool(self.viewCount > 0) if self.viewCount else False`; `isWatched` is
  a plain alias of `isPlayed`.
- `viewCount` (int) and `lastViewedAt` (datetime) live on the base `Video` class;
  `Episode`/`Movie` additionally carry `viewOffset` (int, milliseconds — mid-episode
  resume position, not itself a boolean watched flag).
- Write path (not needed for a read-only progress *sync*, but confirms the shape):
  `markPlayed()`/`markWatched()` hit `/:/scrobble`, `markUnplayed()`/`markUnwatched()`
  hit `/:/unscrobble`, both with `identifier=com.plexapp.plugins.library`.

**XML vs. JSON:** Plex's API defaults to XML; JSON is available on the same endpoints
by sending `Accept: application/json` (confirmed via Plex's own developer docs at
https://developer.plex.tv/pms/ and corroborated by Plexopedia's API reference at
https://www.plexopedia.com/plex-media-server/api/). `python-plexapi` itself parses XML
internally (`PlexObject._loadData` reads `data.attrib`, i.e. ElementTree attributes) —
so using the library means never touching the wire format directly either way. If
#153 ends up hand-rolling requests instead of depending on `python-plexapi` (matching
this app's existing preference for direct API clients over vendored third-party tools
— see `CLAUDE.md`'s "no vendored third-party CLI" note re: `sync_crunchyroll.py`/
`sync_netflix.py`), requesting JSON explicitly is the realistic choice, matching how
`sync_crunchyroll.py`/`sync_netflix.py` already consume JSON APIs rather than parsing
HTML/XML by hand.

**Confidence: high, from source code + a corroborating independent doc source for the
field list.** Not live-tested: the exact response shape for *this app's* eventual
query parameters (e.g. whether `viewedAt>` combined with `librarySectionID` behaves as
expected on a real multi-library server) is standard integration-testing territory,
not a gap in understanding the API's shape.

## 3. AniList-resolvable identifiers on library items

`python-plexapi`'s `media.py` defines a `Guid` class (confirmed reading the class
directly):

```python
class Guid(PlexObject):
    """ Represents a single Guid media tag.
        Attributes:
            TAG (str): 'Guid'
            id (id): The guid for external metadata sources (e.g. IMDB, TMDB, TVDB, MBID).
    """
```

Library items carry both a single `guid` string (Plex's own internal id, e.g.
`plex://movie/5d776b59ad5437001f79c6f8` under Plex's current-generation agents — not
externally resolvable) **and** a `guids` list of these `Guid` objects (plural,
`cached_data_property`, on `Movie`/`Episode`) holding the *actual* external identifiers.
**What identifier shows up depends entirely on which metadata agent matched the item**,
confirmed via web research (not `python-plexapi` itself, which is agent-agnostic and
just parses whatever `id` string the server returns):

- **Default agents** (Plex Movie / Plex TV Series, current-gen) — `guids` populated
  with `imdb://tt...`, `tmdb://...`, `tvdb://...` entries
  (https://forums.plex.tv/t/implemented-native-plex-agents-allow-access-to-external-provider-ids-for-media-eg-imdb-tmdb-tvdb/619090
  confirms this is how the current-gen agents expose provider ids; the pre-current-gen
  format embedded the id directly in `guid` itself, e.g.
  `com.plexapp.agents.imdb://tt0054215`). None of these are AniList- or
  AniDB/MAL-resolvable directly — most anime, scanned under a default TV/Movie agent,
  will carry TVDB/TMDB/IMDb ids only.
- **Anime-specific agents exist and change this**, but require the user to have
  installed and applied one, which is not guaranteed: **HAMA** (HTTP AniDB Metadata
  Agent, https://github.com/ZeroQI/Hama.bundle) produces guids like
  `com.plexapp.agents.hama://anidb-4776` (AniDB id, sourced from AniDB→TVDB mapping
  data), and **MyAnimeList.bundle** (https://github.com/Fribb/MyAnimeList.bundle)
  matches directly against MyAnimeList ids. Neither is AniList ids directly, but both
  are one hop away — AniDB and MAL ids both have well-established community mapping
  tables to AniList ids (the same `Fribb/anime-lists`-style mapping the Netflix/Prime
  research explicitly declined to add as a dependency, see
  `notes/2026-08-14-netflix-prime-sync-research.md`'s Architecture decisions).
- **Not confirmed from any source read in this pass:** how commonly AniDex users'
  actual Plex libraries have HAMA or MyAnimeList.bundle installed vs. running under a
  default agent. That's a real unknown, but it doesn't block a decision — see fallback
  below, which is agent-agnostic by design.

**Decision: title-match heuristic as the primary path, not identifier matching as a
hard dependency.** Same conclusion the Netflix/Prime research reached and the same
shape #98's Netflix CSV import already ships in this codebase — this is now the third
provider integration to land on "title match against the user's own AniList library,
not an external id-mapping table" as the pragmatic answer, which is a real signal this
is the right default for this app rather than a one-off shortcut:

- Reuse `scripts/anilist_sync_common.py`'s existing `fetch_user_list()` /
  `find_anilist_id()` pair — pre-built lowercased title index checked first (zero API
  calls for exact matches), falling back to AniList's search endpoint for anything not
  already in the user's library, with caching (`_search_cache`) and the existing
  90-req/min throttle. This is the same helper Crunchyroll and Netflix sync already
  share; Plex should be the third consumer, not a fourth bespoke matcher.
- `is_plausible_match()` (same file) already guards against the movie/format and
  episode-count-overrun collision cases that matter for a mixed-catalog service — Plex
  libraries, like Netflix/Prime, are not anime-only, so this guard applies the same way
  it does for Netflix/Prime rather than being skipped the way Crunchyroll (anime-only
  catalog) skips it.
- For the colon/subtitle-in-title ambiguity class specifically — confirmed by reading
  `scripts/import_netflix_csv.py`'s `extract_series_title()` docstring directly — #98
  solved this by only trusting a "Series: Episode" colon split when the pre-colon
  segment independently matches a title already in the user's AniList library (guards
  against real movie titles that contain a colon, e.g. "Mission: Impossible"). Plex
  library items don't have this exact problem shape (Plex's own metadata already
  separates series/episode/movie into structured fields — `grandparentTitle` vs.
  `title` — rather than concatenating them into one string the way a Netflix CSV row
  does), so #98's specific colon-splitting code isn't directly reusable, but the
  underlying principle (corroborate an ambiguous heuristic against the user's own
  AniList library before trusting it, rather than trusting the raw string alone) is the
  right pattern to point #153 at.
- **Two-tier fallback for #153, in priority order:** (1) if `guids` contains an AniDB
  or MAL id (HAMA/MyAnimeList.bundle installed), that's a strictly better signal than
  title matching where available — worth checking first opportunistically, not worth
  building special-case mapping-table plumbing for on this pass; (2) title-match via
  `find_anilist_id()` against `grandparentTitle` (episodes) or `title` (movies)
  otherwise, same as Crunchyroll/Netflix/Prime. No AniList match on either path = skip,
  same accepted behavior as every other sync path in this app today.

**Confidence: high on the identifier landscape (agent-dependent, well-documented from
multiple independent community sources) and high on the recommended approach (directly
reuses code already proven across three prior integrations). Not confirmed: what
fraction of a real AniDex user's Plex library would actually hit the agent-id fast
path vs. falling through to title matching** — that's a property of how a specific
user has their server configured, not something a documentation pass can answer, and
it doesn't change the recommendation either way since the title-match fallback has to
exist regardless.

## Acceptance criteria — resolved vs. open

| # | Criterion | Status |
|---|---|---|
| 1 | Findings documented in a `notes/` file | **Resolved** — this file. |
| 2 | Auth model confirmed and a token flow chosen | **Resolved.** Both flows read directly from `python-plexapi` source; OAuth PIN flow chosen over raw server-token paste, with the account→server token exchange mechanism (`resources()`) confirmed as the thing that removes the "paste a raw token" awkwardness. |
| 3 | Watch-history endpoint and response shape confirmed | **Resolved.** `GET /status/sessions/history/all` on the Plex server, method body and field list confirmed from source plus one independent corroborating doc (Plexopedia). Native `viewedAt>` watermark filter confirmed, which is a materially better fit for this app's incremental-sync pattern than Prime Video turned out to be. |
| 4 | Title-matching approach decided, with stated fallback if no anime agent installed | **Resolved.** Reuse `anilist_sync_common.py`'s existing title-index/search-fallback/plausibility-guard trio (already shared by 3 sync paths); agent-id (`guids`/HAMA/MyAnimeList.bundle) checked first when present as a strictly-better opportunistic signal, title match as the guaranteed fallback. |

**None of the four are blocked on a live instance.** What a live instance would still
usefully confirm, purely as ordinary pre-merge verification for #153 (not a gap in
this spike's own acceptance criteria):

- That this app's own PIN-creation/polling implementation, once written, actually
  completes end-to-end against a real plex.tv account (mechanics are confirmed;
  wiring them into this app's request/response cycle is new code that needs testing
  like any new code).
- The real-world mix of default-agent vs. anime-specific-agent libraries this app's
  actual users run — doesn't change the chosen approach, only how often the fast path
  fires versus the guaranteed fallback.
- Exact behavior of combining `viewedAt>` with `librarySectionID`/`accountID` filters
  on a real multi-user, multi-library server (the method signature and single-filter
  behavior are confirmed; combined-filter edge cases are the kind of thing worth a
  quick live smoke test before relying on them in production, same spirit as the
  fastapi/starlette lesson in `CLAUDE.local.md`'s Dependency PR review section — don't
  assume a documented parameter combination behaves exactly as read without seeing it
  once).

## Sources

- [`pkkid/python-plexapi`](https://github.com/pkkid/python-plexapi) — `myplex.py`
  (`MyPlexAccount`, `MyPlexPinLogin`, `MyPlexResource`), `server.py` (`PlexServer`,
  `history()`), `media.py` (`Guid`), `video.py` (`Video`/`Movie`/`Episode` fields),
  `mixins/played_unplayed.py` (`PlayedUnplayedMixin`) — read directly, not summarized
  from README.
- [Plex developer docs](https://developer.plex.tv/pms/) — XML/JSON content negotiation
  via `Accept` header.
- [Plexopedia — Get Session History](https://www.plexopedia.com/plex-media-server/api/server/session-history/)
  and [companion blog post](https://www.plexopedia.com/blog/get-session-history/) —
  independent corroboration of the `/status/sessions/history/all` field list.
- [ZeroQI/Hama.bundle](https://github.com/ZeroQI/Hama.bundle) — HAMA anime agent,
  AniDB-sourced guids.
- [Fribb/MyAnimeList.bundle](https://github.com/Fribb/MyAnimeList.bundle) — MAL-id
  anime agent.
- [Plex forums — native agent external provider ids](https://forums.plex.tv/t/implemented-native-plex-agents-allow-access-to-external-provider-ids-for-media-eg-imdb-tmdb-tvdb/619090) —
  confirms current-gen default agents expose `imdb://`/`tmdb://`/`tvdb://` guids.
- This repo: `app/main.py` (`/settings/credentials`, `/settings/link/{provider}`,
  `/auth/link-callback/{provider}`), `app/templates/settings.html` (CR/Netflix
  credential-form pattern), `scripts/anilist_sync_common.py` (title-index/search
  matching), `scripts/import_netflix_csv.py` (`extract_series_title` colon heuristic),
  `notes/2026-08-14-netflix-prime-sync-research.md` (prior spike, same convention).
