# Netflix/Prime Video sync — research spike findings

Tracking issue: #15. This is the research spike that issue's acceptance criteria call for
("Research spike documents the actual technical approach for both services... with sources")
before any implementation work starts on `scripts/sync_netflix.py` / `scripts/sync_primevideo.py`
(currently `NotImplementedError` stubs, commit `527cbab`).

**Status: Netflix architecture resolved. Prime Video architecture very likely the same shape
(cookie-replay against a JSON API), pending one live-session confirmation of the exact
endpoint** — see "What's still unresolved" below.

## What each service actually exposes

### Netflix

- **Official export**: Netflix's account "Viewing activity" page has a documented,
  GDPR/CCPA-backed "Download all" button producing a full-history CSV, per-profile. This is a
  sanctioned, intended feature — not a private API. Downside: it's all-or-nothing (no
  date-range parameter) and can only be triggered through a logged-in browser session, so
  automating it means automating a *login flow*, not just an API call.
  - [Netflix Help Center — viewing history and device activity](https://help.netflix.com/en/node/101917)
  - [Digital Takeout Day — how to download your Netflix data](https://takeoutday.org/services/netflix)
- **Shakti API (internal, reverse-engineered)**: Netflix's own web/mobile client talks to an
  internal API nicknamed "Shakti." Several OSS clients exist and are actively documented:
  - [`LBBO/node-netflix2`](https://github.com/LBBO/node-netflix2) — Node client with
    `getViewingHistory()`, cookie-based auth, no login automation needed once you have a
    session cookie.
  - [`statsoflife/extract-netflix-activity`](https://github.com/statsoflife/extract-netflix-activity) —
    hits `api/shakti/<build_number>/viewingactivity` directly using Netflix ID cookies.
  - [`oldgalileo/shakti`](https://github.com/oldgalileo/shakti) — breakdown/documentation of
    the API shape itself.
  - [Gist: Netflix's web architecture reverse-engineered from live traffic](https://gist.github.com/sshh12/dda3a89514f850c459380b18b1f7eb7b) —
    broader system context (18 named internal systems, Falcor→GraphQL migration, etc.), useful
    background but not required reading for this integration.
  - The viewing-activity endpoint is paginated, most-recent-first — a natural fit for
    incremental fetching (see below).

### Prime Video

- **No real-time official export.** Amazon's GDPR "Request My Data" is an async, multi-day
  process — not usable for a periodic sync job.
- **Update (follow-up pass): a cookie-authenticated JSON endpoint almost certainly exists,
  confirmed by reading the actual source of the exporter tools rather than just their
  descriptions** — this is a materially better position than the first pass found. Two OSS
  tools found for Prime Video turn out to represent two different eras/approaches:
  - [`gitzain/prime-video-history-to-csv`](https://github.com/gitzain/prime-video-history-to-csv) —
    genuinely Selenium: logs in with username/password via XPath-driven form fill, then scrapes
    rendered DOM nodes. No API involved. Highest-risk tier, as originally assessed.
  - [`twocaretcat/watch-history-exporter-for-amazon-prime-video`](https://github.com/twocaretcat/watch-history-exporter-for-amazon-prime-video) —
    its own README ("How it Works" section) and source confirm the `primevideo.com/settings/watch-history`
    page is **not** server-rendered HTML per page load. The first chunk of history ships inline
    as a JSON blob in a `<script type="text/template">` tag; scrolling further triggers real
    `fetch()` calls (the script monkey-patches `window.fetch` to intercept them) that return
    more JSON in the identical shape: a `widgets` array, filtered to `widgetType === "watch-history"`,
    containing date-sectioned `titles` with a `gti` (Amazon's "Global Title Identifier" — e.g.
    `amzn1.dv.gti.83c8ce44-b42e-40fd-8546-c36fd2824071`), episode children with their own `gti`,
    watched timestamps, and title/episode paths. That is structurally the same shape as
    Netflix's Shakti viewing-activity response — a paginated, cookie-authenticated JSON API
    backing the account's own history page, not raw HTML scraping.
  - **What's not yet confirmed**: the literal endpoint URL/query parameters. This script
    doesn't hardcode them — it passively intercepts whatever `fetch()` calls the page's own JS
    makes as you scroll, which is why it never needed to know the URL. General web search
    (multiple queries: endpoint-name guesses, "reverse engineering" + Prime Video, Video
    Central/Avails API docs — which are for content-provider partners, unrelated to consumer
    watch history) did not surface anyone's write-up of the actual URL/params. Confirming it
    requires a live authenticated session: open `primevideo.com/settings/watch-history` in a
    browser, open devtools' Network tab, scroll, and read off the request the page itself
    makes — the same way the Shakti endpoint was originally found by others for Netflix.
  - This meaningfully changes the risk picture from the first pass: Prime Video is likely
    **not** stuck at "Selenium or nothing" — a cookie-replay approach analogous to the Netflix
    plan is plausible pending that one confirmation step. Until the URL is confirmed, treat
    this as "very likely, not yet verified," not as a settled decision.
  - This is the highest-risk tier of everything surveyed if it does turn out Selenium is the
    only path: Amazon's bot/fraud detection is tied to the same stack protecting its payments
    surface, and Selenium leaves the largest
    fingerprint (WebDriver flags, full page render) of any approach considered.

### Prior art worth knowing about but not adopted

- **Universal Trakt Scrobbler** ([`trakt-tools/universal-trakt-scrobbler`](https://github.com/trakt-tools/universal-trakt-scrobbler)) —
  a large, multi-year browser extension that already does "watch Netflix/Prime/etc. and sync to
  Trakt" for a big install base. Considered as a possible intermediary (sync from Trakt's API
  instead of building direct integrations) but not adopted — see "Architecture decisions"
  below. Its issue tracker was also the single most useful data point in the risk assessment
  (see next section).
- **Trakt to Trakt tools generally** (`Netflix-to-Trakt-Import`, `Crunchflix`) — same category,
  not directly reused, but confirm this is a well-trodden problem space with multiple
  independent implementations, none of which report account-level consequences as a common
  failure mode.

## Risk assessment

Three genuinely different risk categories, kept separate rather than lumped under "ToS
violation":

1. **Contractual (ToS breach)** — real, but it's a contract matter between the account holder
   and the service, not a criminal one. Enforcement in practice targets commercial-scale
   actors (data resale, competing services), not individuals polling their own account at low
   volume.
2. **Legal (CFAA / unauthorized-access statutes)** — *Van Buren v. United States* and EFF's
   position both support that a ToS breach alone does not constitute "unauthorized access"
   under the CFAA when it's your own account. *hiQ Labs v. LinkedIn* trends the same direction
   for login-gated personal data, even though it was decided on public-data scraping
   specifically.
   - [EFF: Violating Terms of Service Isn't a Crime Under the CFAA](https://tagteam.harvard.edu/hub_feeds/3623/feed_items/2727047/about)
3. **Practical (anti-bot / fraud heuristics)** — this is the one that actually matters
   day-to-day, and it's frequency/volume-triggered, not intent-triggered. Concrete evidence
   found:
   - Netflix's own device-registration heuristics can trigger a **temporary** account lock
     under high-frequency unofficial-API use (read as "too many devices," not as "scraping") —
     self-resolving, not a ban.
   - No credible reports found, on either service, of a *permanent* ban resulting from
     personal-use watch-history scraping.
   - **Universal Trakt Scrobbler's issue tracker** — the single strongest data point available.
     A tool at far greater scale (multi-year, large install base) doing exactly this for both
     Netflix and Prime Video has an issue tracker dominated by "sync broke," "not logged in,"
     "Netflix changed something" — technical fragility, not a single report found of an
     account being flagged or banned as a consequence of using it.
     ([issues](https://github.com/trakt-tools/universal-trakt-scrobbler/issues))
   - **`crunchyexporter-cli`** — already vendored and running in this repo's production against
     a real Crunchyroll account (see Dockerfile, pinned commit
     `1855e567ad1704a6655feedffcf76b1d77e5d690`) — has no ban or rate-limit issues surfaced in
     its own tracker.

**Conclusion**: across every example surveyed, the dominant real-world failure mode is *the
scraper breaking when the site changes*, not *the account being punished*. Engineering effort
should prioritize resilience to upstream changes over obfuscation against detection. The one
genuine, documented account-consequence mechanism (Netflix's device-limit-style temporary lock)
is exactly the failure mode incremental, low-frequency (e.g. daily) syncing avoids.

## Architecture decisions

- **Netflix**: build against the **Shakti API, cookie-replay auth** — same shape as the
  existing Crunchyroll integration (`sync_crunchyroll.py` + `crunchyexporter-cli`'s `etp_rt`
  cookie pattern). Chosen over the official CSV export specifically *because* the CSV route
  requires automating a login flow — the single highest-scrutiny surface on either service —
  while cookie replay never touches it. The CSV export's official/sanctioned status doesn't
  outweigh that automation-surface difference.
- **Prime Video**: **likely the same cookie-replay shape as Netflix, pending one confirmation
  step.** A follow-up pass reading the actual source of `twocaretcat/watch-history-exporter-for-amazon-prime-video`
  (not just its description) found strong structural evidence of a cookie-authenticated JSON
  API behind `primevideo.com/settings/watch-history` — paginated, keyed by Amazon's `gti`
  identifiers, same shape as Shakti — but the literal endpoint URL/params aren't confirmed from
  any published source. See "What's still unresolved."
- **No Trakt intermediary.** Direct integration, matching the existing Crunchyroll precedent
  and issue #15's own stated preference for `scripts/sync_netflix.py` /
  `scripts/sync_primevideo.py` over any new module tree. A Trakt-mediated design was
  considered and rejected: it would require the user to keep a browser extension running
  continuously and would make this app dependent on Trakt as a trust boundary for personal
  watch data, for no clear benefit over a direct integration once the direct-integration risk
  picture came back this favorable.
- **Anime matching**: reuse the existing pattern from `sync_crunchyroll.py`
  (`fetch_user_list()` / `find_anilist_id()` — a pre-built title index from the user's AniList
  library, falling back to AniList's search endpoint for unrecognised titles, with caching and
  the existing 90-req/min throttle). No new external ID-mapping dependency
  (`Fribb/anime-lists`, `Otaku-Mappings`, `ids.moe`, etc.) — the CR integration already solves
  this class of problem without one, and "no AniList match" is the natural non-anime filter
  too (a Netflix/Prime title that isn't in the user's AniList library, or isn't found via
  search, is simply skipped — same as CR sync does today for unmatched titles).
- **Incremental sync design**: fetch **newest-first, stop once a per-service watermark is
  reached** — not "pull full history every sync." This mirrors the role `cr_sync_state`
  already plays for Crunchyroll, but the watermark needs to be enforced *at fetch time*
  (bounding how many upstream requests get made), not just used as a post-hoc local diff after
  a full pull. Netflix's paginated Shakti endpoint and Prime's most-recent-first history page
  both support this directly. The Netflix CSV export path structurally *cannot* — it's an
  all-or-nothing dump with no date-range parameter — which is itself a point against it beyond
  the login-automation concern above.
  - Note: this is the same incremental-fetch gap identified in the currently-live Crunchyroll
    integration while researching this — see the new tracking issue filed for that
    (referenced from a comment on #15).
- **Module layout**: already scaffolded on this branch (`scripts/sync_netflix.py`,
  `scripts/sync_primevideo.py`, `NotImplementedError` stubs, commit `527cbab`) — matches the
  decision above, nothing to change.

## What's still unresolved

- Real per-service rate limits / device-lock thresholds aren't documented anywhere found — the
  Netflix device-lock report above is qualitative ("frequent requests"), not a number. Worth
  staying conservative (daily sync cadence, small watermark-bounded requests) rather than
  trying to find the actual threshold empirically.
- ~~Server-side replay not yet confirmed~~ **RESOLVED 2026-08-26** — ran
  `scripts/dev/probe_primevideo_history.py` against a live account. Plain `httpx` with the
  captured cookie header works cleanly: no TLS-fingerprint/bot-detection block, no CAPTCHA, no
  403. Walked 3 pages, all HTTP 200, real watch history returned including real anime titles
  (MADE IN ABYSS, The Demon Sword Master of Excalibur Academy, From Old Country Bumpkin to
  Master Swordsman II) — confirms the cookie-replay approach is viable end-to-end, not just
  theoretically plausible.
- ~~Not yet confirmed: whether page 1 comes through this endpoint~~ **RESOLVED** — a cold call
  with no `nextToken` at all returned HTTP 200 with real page-1 data. This endpoint serves the
  *entire* history, not just scroll-triggered pages 2+. Simpler than assumed: no special
  first-page-is-inline-HTML case to handle, `sync_primevideo.py` can always call this one
  endpoint, optionally with a `nextToken`, full stop.
- **New finding from the deeper walk**: a season's episodes can split across **pagination
  boundaries**, not just across date-sections within one page — confirmed live:
  "REACHER (TV) - SEASON 01" (`gti amzn1.dv.gti.d62095f9-...`) shows episodes 8 down to 2 on
  page 2, and episode 1 only appears on page 3. This matters less than it first sounds for the
  *steady-state incremental* case (walk newest-first, stop at the stored watermark, and for
  each season `gti` seen take the max parsed episode number among newly-seen events, then
  `progress = max(existing_AniList_progress, that_number)` — never needs full-season
  aggregation, same "never regress progress" discipline already used for CR/Netflix) — but it
  does matter for the **very first sync** (`full_pull=True`, no watermark yet), which has to
  walk deep enough into history and aggregate every occurrence of a season's `gti` across
  every page to find its true max episode, the same class of problem Crunchyroll's
  max-aggregated-history fetch already solves, not a new one.

## Status: ready for implementation

Every open question in this file is now resolved. `scripts/sync_primevideo.py` can be built for
real against the confirmed endpoint/pagination/response shape above, reusing
`resolve_or_create_user_list_entry()`/`ensure_anime_stub()`/`enqueue_outbox_update()` from
`anilist_sync_common.py` per issue #17's rewritten scope — see that issue for the full
acceptance criteria.

## Prime Video endpoint — CONFIRMED (2026-08-26, live capture)

Captured directly from a real logged-in browser session (devtools `fetch()` interception,
following the `twocaretcat` tool's own technique) against `primevideo.com/settings/watch-history`.

**Request**: `GET /api/getWatchHistorySettingsPage?widgetArgs=<urlencoded JSON>`

`widgetArgs` is `{"nextToken": "<base64>"}`. `nextToken` itself base64-decodes to:

```json
{"version": "V2", "paginationInfo": "<base64>"}
```

...which base64-decodes again to a **DynamoDB-shaped** cursor (the `{"S": "..."}` wrapper is
literally DynamoDB's own AttributeValue string-type serialization — this endpoint reads
straight off a DynamoDB table, "hot storage" in Amazon's own naming):

```json
{
  "timeStamp": 1787737926.27...,
  "hotStoragePaginationToken": {
    "TimeInterval": {"S": "2026-07-12-21"},
    "lastModifiedDate": {"S": "2026-07-12T21:46:54.426580404Z"},
    "lastTitleId": {"S": "amzn1.dv.gti.77650506-dee2-4b53-a87e-189f8ed5cd9c"}
  }
}
```

This is a genuine keyset cursor — `lastModifiedDate` + `lastTitleId` is the natural watermark
pair for `primevideo_sync_state`, more precise than Netflix's date-only watermark.

**Auth**: cookies only, same as Netflix/Crunchyroll — no bearer token seen. The page's own JS
sends `x-requested-with: XMLHttpRequest` on the request; `x-amzn-requestid` and
`X-Amzn-Client-TTL-Seconds` are almost certainly *response* headers (standard AWS API Gateway
tracing/cache-control headers), not something the client needs to send — confirm this doesn't
matter once `probe_primevideo_history.py` is actually run, rather than assuming.

**Response shape** — the widget of interest is `widgets[]` where `widgetType == "watch-history"`:

```
content.content.titles: [
  { date: "August 2, 2026",         # human date-section header, newest-first
    titles: [
      { gti, title: {href, text}, titleType: "movie"|"season", time: <epoch ms>,
        children: [
          { gti, title: {href, text: "Episode N: <ep title>"}, titleType: "episode",
            time: <epoch ms>, children: [] },
          ...
        ] },
      ...
    ] },
  ...
]
content.content.nextToken: "<base64, same shape as the request's own nextToken>"
```

Key structural findings that matter for implementation:

- **Newest-first, date-sectioned** — confirms the same watermark-bounded incremental-fetch
  design already decided above works directly; no design change needed there.
- **A season's `gti` recurs across MULTIPLE date-sections** if watched across multiple days —
  confirmed live: one season (`gti amzn1.dv.gti.d62095f9-...`, "REACHER (TV) - SEASON 01")
  appears under 5 different date-sections, each time with only that day's newly-watched
  episodes under `children`. **Progress for a season is not readable from any single entry** —
  it has to be computed by aggregating `children[]` across every occurrence of that season's
  `gti` (within the fetched page range), not read off one row the way Crunchyroll's
  max-aggregated history allows in a single field.
- **Each episode carries its own exact `gti`, watch timestamp, and `title.text` in the literal
  form `"Episode N: <episode title>"`.** This is materially better than Netflix's shape —
  Netflix has no absolute episode-ordinal field at all (hence its fragile "count distinct new
  episodes" delta heuristic, see `sync_netflix.py`'s docstring). Prime Video's episode number
  can be parsed directly from `title.text` via a simple `^Episode (\d+):` match and used as an
  **absolute** progress value, matching Crunchyroll's approach rather than Netflix's — a
  materially safer design than what was originally assumed here ("mirror Netflix's shape").
- **Movies** (`titleType: "movie"`) are standalone, `children: []`, matching the existing
  movie-vs-series branch already established for Netflix/CR (progress=1/COMPLETED, not
  WATCHING).
- **Season title text is inconsistent** — same show can show as `"Reacher"` for one season's
  `gti` and `"REACHER (TV) - SEASON 01"` for a different season's `gti` of what's presumably
  the same underlying show, and at least one observed entry showed only `"Season 3"` with no
  show name attached at all. This is the same class of problem #159 (CR season-mismatch) and
  the existing `season_suffix_candidates()`/`is_plausible_match()` guard in
  `anilist_sync_common.py` already exist to handle — expect to lean on them directly, and don't
  be surprised if some Prime Video entries simply fail to resolve to an AniList id and get
  skipped, same as CR/Netflix already do today for anything that doesn't match.
- Two different `gti`s for what's presumably the same show's two different seasons is actually
  a *good* structural match for AniList's own per-season-media-entry model — each Prime Video
  season `gti` should map to at most one AniList media id, same as Crunchyroll already assumes.

**Not yet confirmed**: whether page 1 (no `nextToken`) also comes through this same endpoint,
or arrives inline in the initial page HTML with this endpoint only firing on scroll (page 2+).
Every capture so far already had a `nextToken` present (mid-pagination) — worth checking
directly against `scripts/dev/probe_primevideo_history.py`'s first request (call with no
`nextToken` at all and see what comes back) rather than needing another browser capture.
