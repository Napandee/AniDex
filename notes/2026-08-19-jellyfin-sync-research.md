# Jellyfin sync — research spike findings

Tracking issue: #150 (spike blocking the Jellyfin implementation issue, #152; parent epic
#148). This is the research pass #150's acceptance criteria call for
("Findings documented in a `notes/` file... Auth model confirmed... Watched-status/history
endpoint and response shape confirmed... Title-matching approach decided").

**Scope correction from the issue title:** unlike Netflix/Prime Video (no public API — #17
genuinely needed a live traffic capture), Jellyfin publishes a full public OpenAPI spec and
its server source is open on GitHub (`jellyfin/jellyfin`) with the actual controller code
and DTOs. This pass had **no access to a live Jellyfin server** and used none — it is a
documentation/source-code research pass, reading the real server source, the DTO
definitions, and a well-established real-world Jellyfin↔AniList sync plugin
(`vosmiic/jellyfin-ani-sync`) the same way the Netflix/Prime spike (#15,
`notes/2026-08-14-netflix-prime-sync-research.md`) got most of the way there by reading
`node-netflix2` / `watch-history-exporter-for-amazon-prime-video`'s actual source before any
live capture was needed.

**Status: auth model and endpoint shape resolved with high confidence from primary sources.
Title-matching direction resolved (title-heuristic is the realistic default-case path,
mirroring #98's Netflix CSV pattern) — but confirming a default Jellyfin+AniDB-plugin
install's exact `ProviderIds` contents, and validating any of this against a real
request/response, both still need a live instance.** See "What's still unresolved" below.

## 1. Auth model

Confirmed from the Jellyfin server's own controller source and a Jellyfin core developer's
published API-authorization reference:

- **API key generation**: `Jellyfin.Api/Controllers/ApiKeyController.cs` exposes
  `GET /Auth/Keys` (list), `POST /Auth/Keys?app={name}` (create), and
  `DELETE /Auth/Keys/{key}` (revoke) — all three gated
  `[Authorize(Policy = Policies.RequiresElevation)]`, i.e. admin-only. In practice this is
  the same thing the Dashboard → API Keys page in the web UI does; there's no
  non-admin/self-service key creation.
  ([`ApiKeyController.cs`](https://github.com/jellyfin/jellyfin/blob/master/Jellyfin.Api/Controllers/ApiKeyController.cs))
- **Using an API key**: sent as an HTTP header, not a bearer token in the OAuth sense.
  Jellyfin's own OpenAPI security definition registers `X-Emby-Token` as an API-key-style
  header, and it also accepts the same value via the general `Authorization` header in the
  `MediaBrowser Token="<key>"` format — confirmed by a Jellyfin core developer's own
  API-authorization write-up: *"Sending secure data in a query parameter is unsafe"* (it
  explicitly discourages the `?ApiKey=` query-string form still accepted for
  backwards-compatibility) *"...recommends using the Authorization header instead"*, format
  `Authorization: MediaBrowser Token="[key]"`.
  ([nielsvanvelzen — Jellyfin API Authorization gist](https://gist.github.com/nielsvanvelzen/ea047d9028f676185832e51ffaf12a6f),
  cross-checked against search-result summaries of Jellyfin's own OpenAPI security scheme
  and the `X-Emby-Authorization`/`X-Emby-Token` header names used throughout the server
  source and the DeepWiki API-startup writeup)
- **The fuller client-identification form** of the same header —
  `MediaBrowser Client="ClientName", Device="DeviceName", DeviceId="UniqueDeviceId",
  Version="1.0.0", Token="AccessToken"` — is what a full interactive client (not a bare
  server-to-server API key) sends; `jellyfin-apiclient-python`'s `get_default_headers()`
  builds exactly this and appends `Token="<AccessToken>"` once authenticated.
- **User login** (needed only if going the access-token route instead of a static admin API
  key): `POST /Users/AuthenticateByName` with `{"Username": ..., "Pw": ...}`, returning an
  `AuthenticationResult` containing an `AccessToken`. Confirmed directly from
  `jellyfin-apiclient-python`'s `login()`:
  ```python
  def login(self, server_url, username, password="", session=None):
      path = "Users/AuthenticateByName"
      authData = {
          "username": username,
          "Pw": password
      }
  ```
  ([`jellyfin_apiclient_python/api.py`](https://raw.githubusercontent.com/jellyfin/jellyfin-apiclient-python/master/jellyfin_apiclient_python/api.py))
- **Recommendation for this app's shape**: a single admin-generated API key
  (`POST /Auth/Keys`), stored per-user the same way Crunchyroll/Netflix credentials are
  today, is simpler than the username/password → access-token flow — it sidesteps storing a
  Jellyfin password at all, at the cost of needing a Jellyfin *admin* to generate the key
  rather than the end user self-serving it (same asymmetry CR/Netflix's cookie-capture flow
  already has, just via a different admin surface). This is a design call for #152, not
  something the research changes — flagging it here since the endpoint list above is what
  makes either option possible.

**Confidence: high.** All three pieces (key generation endpoint + admin-only policy, header
format, login endpoint + request/response shape) are read directly from Jellyfin's own
controller source or a working reference client's source, not paraphrased from a blog post
summary alone. Nothing here needs a live instance to trust.

## 2. Watched-status / history endpoint and response shape

The issue named two candidates. Reading the actual controller source resolves which one is
real:

### `PlaystateController` — write-only, not a fit

`Jellyfin.Api/Controllers/PlaystateController.cs` has **no endpoint that returns a list of
watched items or playback history**. Every route in it is a *write* (mutate one item's played
state or report a live playback session), not a *read*:

| Method | Verb | Route | Purpose |
|---|---|---|---|
| `MarkPlayedItem` | POST | `UserPlayedItems/{itemId}` | mark one item played |
| `MarkUnplayedItem` | DELETE | `UserPlayedItems/{itemId}` | mark one item unplayed |
| `MarkPlayedItemLegacy` | POST | `Users/{userId}/PlayedItems/{itemId}` | deprecated form of the above |
| `ReportPlaybackStart`/`Progress`/`Stopped` | POST | `Sessions/Playing[...]` | live session telemetry, not a queryable history |
| `PingPlaybackSession` | POST | `Sessions/Playing/Ping` | keep-alive for an active session |

None of these hand back "give me everything user X has watched." Sessions/Playback
Reporting is for *live* session state (what's playing right now, across devices) — Jellyfin
does not ship a first-party endpoint that returns a durable playback-history log the way
Netflix's Shakti `viewingactivity` or Crunchyroll's watch-history API do. (A separate
community "Playback Reporting" *plugin* exists for admin analytics dashboards, but it's an
optional plugin with its own DB tables and API surface, not something to build the core sync
against — not investigated further here since the `/Items` path below already answers the
need without it.)

### `GET /Items` (formerly `Users/{userId}/Items`) — the actual answer

`Jellyfin.Api/Controllers/ItemsController.cs`:

```csharp
[Route("")]
[Authorize]
[Tags("Library")]
public class ItemsController : BaseJellyfinApiController
```

```csharp
/// <summary>
/// Gets items based on a query.
/// </summary>
[HttpGet("Items")]
public async Task<ActionResult<QueryResult<BaseItemDto>>> GetItems(
    [FromQuery] Guid? userId,
    ... // 70+ query params
    [FromQuery] bool enableTotalRecordCount = true,
    [FromQuery] bool? enableImages = true)
```

with a second, explicitly-deprecated route mapping to the same handler:

```csharp
[HttpGet("Users/{userId}/Items")]
[Obsolete("Kept for backwards compatibility")]
[ApiExplorerSettings(IgnoreApi = true)]
```

So `GET /Items?userId={id}&...` is the current/documented form; `GET /Users/{userId}/Items`
still works (many still-maintained clients, including `jellyfin-apiclient-python`'s
`get_user_items()`, use the older path) but is hidden from the OpenAPI/Swagger UI and
flagged obsolete in the source. New code should use `GET /Items` with `userId` as a query
param.

**Request shape for "what has this user watched / partially watched":**

```
GET /Items?userId={userId}
    &filters=IsPlayed          (or IsResumable for in-progress)
    &recursive=true
    &includeItemTypes=Series,Episode,Movie
    &fields=ProviderIds,SeriesId,SeriesName
    &enableUserData=true
```

- `filters` takes an `ItemFilter[]`, comma-delimited; documented values include
  `IsFolder, IsNotFolder, IsUnplayed, IsPlayed, IsFavorite, IsResumable, Likes, Dislikes`.
  `IsPlayed` → fully watched; `IsResumable` → has a saved playback position but isn't marked
  played (i.e. "in progress" — this is the episode-level-progress case the issue's
  acceptance criteria calls out). There's a known, currently-open upstream bug
  ([jellyfin/jellyfin#16297](https://github.com/jellyfin/jellyfin/issues/16297)) where
  requesting `IsPlayed` and `IsUnplayed` *together* silently drops the first — not relevant
  to a "watched" *or* "resumable" query run separately, but worth knowing if a future version
  ever tries to fetch both in one call.
- `recursive=true` is required to get episodes back when querying at the library/series
  level rather than one series at a time.
- `enableUserData=true` (default `true` per the fetched signature, but worth setting
  explicitly) is what causes each returned item to carry a populated `UserData` object;
  `fields=...` (an `ItemFields[]`) is what's needed to get `ProviderIds` back in the same
  response, since Jellyfin only includes the fields you ask for beyond a lean default set.

**Response shape** — `QueryResult<BaseItemDto>`, i.e. `{ "Items": [BaseItemDto, ...],
"TotalRecordCount": int, "StartIndex": int }`. Relevant `BaseItemDto` fields, quoted from
the actual DTO source
([`BaseItemDto.cs`](https://raw.githubusercontent.com/jellyfin/jellyfin/master/MediaBrowser.Model/Dto/BaseItemDto.cs)):

```csharp
/// <summary>Gets or sets the user data for this item based on the user it's being requested for.</summary>
public UserItemDataDto UserData { get; set; }

/// <summary>Gets or sets the provider ids.</summary>
public Dictionary<string, string> ProviderIds { get; set; }

/// <summary>Gets or sets the type.</summary>
public BaseItemKind Type { get; set; }

/// <summary>Gets or sets the index number.</summary>
public int? IndexNumber { get; set; }          // episode number within its season

/// <summary>Gets or sets the parent index number.</summary>
public int? ParentIndexNumber { get; set; }     // season number

/// <summary>Gets or sets the name of the series.</summary>
public string SeriesName { get; set; }

/// <summary>Gets or sets the series id.</summary>
public Guid? SeriesId { get; set; }
```

And the embedded `UserItemDataDto` (per-user watched/progress state for that item):

| Property | Type | Meaning |
|---|---|---|
| `Played` | `bool` | fully watched |
| `PlayedPercentage` | `double?` | progress through the item |
| `PlaybackPositionTicks` | `long` | resume position (100ns ticks) |
| `PlayCount` | `int` | number of completions |
| `LastPlayedDate` | `DateTime?` | last-watched timestamp — the natural analogue of
  `cr_sync_state.last_seen_watched_at`/`netflix_sync_state.last_seen_watched_at` for a
  per-user Jellyfin watermark |
| `IsFavorite` / `Likes` | `bool` / `bool?` | not needed for sync |

This gives everything #150's acceptance criteria asks for from a single call per user:
episode-level identity (`SeriesName`/`SeriesId` + `IndexNumber`/`ParentIndexNumber`),
watched/in-progress state (`UserData.Played`/`PlayedPercentage`), a timestamp usable as an
incremental watermark (`UserData.LastPlayedDate`), and whatever cross-reference metadata IDs
the library item happens to carry (`ProviderIds`) — see next section for what's actually in
that dictionary by default.

**Confidence: high** that `/Items` with `Filters=IsPlayed`/`IsResumable` +
`enableUserData=true` is the right endpoint and that the field list above is accurate — all
read directly from the controller and DTO source, cross-checked against
`jellyfin-apiclient-python`'s working `get_user_items()`/`item_played()` implementation.
**Not yet confirmed**: the literal JSON response of a real call (field casing, whether every
field documented in the C# DTO is actually non-null/present on a real anime library's items,
pagination behavior on a large library). That's the live-instance gap — see below.

## 3. AniList-resolvable identifiers on library items

This is the question the issue itself flagged as most likely to need a real instance, and
the research here explains *why* rather than resolving it outright.

**Default (non-anime-specific) install**: a stock Jellyfin TV-library install uses TheTVDB
(and optionally TMDB) as its metadata provider. `ProviderIds` on a `BaseItemDto` is a bare
`Dictionary<string, string>` keyed by whatever provider plugin populated it — for a default
install that means keys like `Tvdb`/`Tmdb`/`Imdb`, **not** `AniDb` and never `AniList`.
There is no native "AniList ID" field anywhere in Jellyfin's own DTOs — confirmed by reading
`BaseItemDto.cs` itself (only a generic `ProviderIds` dict, no AniList-specific property).

**With the official AniDB plugin installed**
([`jellyfin/jellyfin-plugin-anidb`](https://github.com/jellyfin/jellyfin-plugin-anidb)):
adds an anime-specific metadata provider that populates `ProviderIds["AniDB"]` (the exact
string constant, read from the plugin's own source:
[`ProviderNames.cs`](https://github.com/jellyfin/jellyfin-plugin-anidb/blob/master/Jellyfin.Plugin.AniDB/Providers/ProviderNames.cs) —
`public const string AniDb = "AniDB";`). This gets you an AniDB ID, **still not an AniList
ID** — AniDB and AniList are different catalogs with different ID spaces.

**Getting from an AniDB ID to an AniList ID still needs a third external lookup.** The most
directly relevant piece of prior art found: **`vosmiic/jellyfin-ani-sync`**
([github.com/vosmiic/jellyfin-ani-sync](https://github.com/vosmiic/jellyfin-ani-sync)) is a
real, maintained Jellyfin *plugin* (not a REST client — it's C# running in-process inside
the Jellyfin server, hooking `IUserDataManager.UserDataSaved` directly) that already does
exactly what #152 wants to do: sync Jellyfin watch progress to AniList (and MAL, Kitsu,
Simkl, others). Reading its actual matching code
(`Helpers/AnimeListHelpers.cs`, `Helpers/AnimeOfflineDatabaseHelpers.cs`) shows its full
chain, and it needs *two* external dependencies beyond Jellyfin itself to get there:

1. A per-anime **AnimeList XML mapping file** (the same class of community-maintained
   AniDB↔TVDB↔season/episode-offset mapping repo this project's own #98/Netflix research
   deliberately declined to add as a dependency — see
   `notes/2026-08-14-netflix-prime-sync-research.md`'s "Anime matching" decision) — used to
   resolve `ProviderIds["Anidb"]` (or, absent that, `ProviderIds["Tvdb"]`) plus season/episode
   numbers down to a bare AniDB ID and an episode offset:
   ```csharp
   if (providers.ContainsKey("Anidb")) {
       logger.LogInformation("(Anidb) Anime already has AniDb ID; no need to look it up");
       ...
   }
   // falls back to a Tvdb-keyed lookup in the same XML mapping if no Anidb id present
   ```
2. Once it has an AniDB ID, a **separate public web API** (`arm.haglund.dev`, "Anime
   Relations Mapper," built on the community `manami-project/anime-offline-database`) to
   convert *that* into an AniList/MAL/Kitsu ID:
   ```csharp
   // See https://arm.haglund.dev/docs#tag/v2/operation/v2-getIds
   response = await httpClient.GetAsync($"{baseUrl}/api/v2/ids?source={source}&id={metadataId}");
   ```
   with the response deserializing straight to `{"anilist": int?, "anidb": int?,
   "myanimelist": int?, "kitsu": int?}`.

**What this establishes**: even the real, working, community-maintained tool that already
solves "Jellyfin watch state → AniList" needs an anime-specific metadata plugin (AniDB) *and*
a third-party ID-crosswalk API to do it by identifier — there is no path where a default
Jellyfin install's own metadata hands back something AniList-resolvable directly. That
confirms the issue's own framing was right, and validates the direction #98's Netflix CSV
import already established for this exact class of problem in this codebase: **title-match
against `anime.title_romaji`/`anime.title_english` is the realistic default-case path, not
just a fallback for an edge case** — for a Jellyfin library without the AniDB plugin (very
plausible for anyone whose library isn't anime-only), it's the *only* path short of adding
new external dependencies (an ID-mapping plugin requirement and/or a third-party crosswalk
API) that this project has so far chosen not to take for CR/Netflix.

**Recommended approach for #152**, following the existing pattern rather than introducing a
new one:

- Primary: reuse `find_anilist_id()` / `is_plausible_match()` from
  `scripts/anilist_sync_common.py` unchanged — `BaseItemDto.SeriesName` (or, for a
  standalone movie, `Name`) is the input, exactly the same shape Netflix/CR sync already
  feeds it. No new matching code needed, just a new caller.
- Optional accelerant, not a requirement: if `ProviderIds["AniDB"]` is present (AniDB plugin
  installed) *and* a future need justifies the added dependency, an `arm.haglund.dev` lookup
  (`GET /api/v2/ids?source=anidb&id={id}` → `.anilist`) could skip title-matching entirely
  for that item. Not needed for a first cut and not proposed as in-scope for #152 — flagging
  it here as the option that exists, matching this project's existing bias (see the
  Netflix/Prime research doc) against adding external ID-mapping dependencies without a
  concrete reason.
- `extract_series_title()`'s colon-hierarchy heuristic from
  `scripts/import_netflix_csv.py` doesn't directly transfer — Jellyfin's `BaseItemDto`
  already gives structured `SeriesName`/`IndexNumber`/`ParentIndexNumber` fields for
  TV content (no colon-string-splitting needed, since Jellyfin's own library structure
  already separates series/season/episode) — but the *shape* of the fallback (structured
  data when available, corroborate an ambiguous case against the user's own AniList library
  before trusting it, otherwise fall through to `find_anilist_id()`'s title-index-then-search
  behavior) is exactly the model to reuse for whatever ambiguity Jellyfin data does turn up
  (e.g. a movie with no season/episode numbers, or a "Season 0" specials bucket that
  shouldn't be treated as a real season for matching purposes — genuinely worth checking
  against a live instance, see below).

**Confidence: high** that a default install does not carry AniList IDs, and that the AniDB
plugin's own `ProviderIds` key is the literal string `"AniDB"`, and that even
`jellyfin-ani-sync` — the closest real-world analog to what #152 needs to build — needs an
AniDB plugin *and* a third-party crosswalk to get an AniList ID by identifier at all.
**Lower confidence / genuinely open**: whether a *default* (no AniDB plugin) install's
`ProviderIds` for anime content reliably contains *any* usable ID (TheTVDB does catalog a lot
of anime under its own IDs, just not AniList's), and what a real anime library's `SeriesName`
values actually look like in practice (romanization variant, dub/sub suffix, "Season 2" in
the series name vs. a distinct season entity) — that's a live-library-content question no
amount of reading the server's C# source can answer, since it depends on how the specific
metadata provider populates real titles, not on the API shape.

## What's still unresolved without a live instance

Per #150's four acceptance criteria:

- [x] **Findings documented in a `notes/` file** — this file.
- [x] **Auth model confirmed (API key generation + usage)** — fully resolved by source
  reading (`ApiKeyController.cs` + the header-format reference above). No live-instance gap.
- [x] **Watched-status/history endpoint and response shape confirmed** — resolved to high
  confidence: `GET /Items?userId=...&filters=IsPlayed|IsResumable&enableUserData=true` is
  the right call, `PlaystateController` ruled out as read-only-incapable, full field list
  read from `BaseItemDto`/`UserItemDataDto` source. **Open gap**: the literal JSON a real
  server returns (exact key casing, which documented fields are actually populated for a
  real anime library vs. theoretically present in the DTO, pagination cursor behavior past
  Jellyfin's default page size) is not verified against a live response. Source-level
  confidence is high; wire-level confidence is not yet earned.
- [x] **Title-matching approach decided (heuristic vs. relying on an anime metadata agent),
  with a stated fallback if the agent isn't installed** — decided: title-match via the
  existing `find_anilist_id()`/`is_plausible_match()` path is the primary approach (not
  merely the fallback), since even a real AniDB-plugin-plus-crosswalk setup
  (`jellyfin-ani-sync`) needs external dependencies this project has so far avoided for the
  same class of problem. **Open gap**: whether a *default* install's `SeriesName` values are
  clean enough for straightforward title matching, or need Jellyfin-specific normalization
  (dub/sub markers, "(TV)" suffixes some scrapers add, a season baked into the series name)
  the way Netflix's colon-hierarchy needed its own heuristic — genuinely unknowable without
  looking at real library data from an actual populated Jellyfin instance.

**Bottom line**: auth and the endpoint/response *shape* are resolved with source-level
confidence and don't need a live instance to trust. The title-matching *direction* is also
decided (reuse the existing AniList-matching pipeline as primary, not a last-resort
fallback) — but, same as the Netflix/Prime spike's Prime Video finding, the exact
wire-level response and the real-world cleanliness of Jellyfin `SeriesName` values are
things only a live instance can settle. #152 should budget for a short live-verification
pass (stand up or use an existing Jellyfin instance with an anime library, hit
`GET /Items` for real, eyeball actual `SeriesName`/`ProviderIds` values) before or during
implementation, not treat this spike as having made that step unnecessary.
