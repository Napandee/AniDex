# Netflix/Prime Video sync — research spike findings

Tracking issue: #15. This is the research spike that issue's acceptance criteria call for
("Research spike documents the actual technical approach for both services... with sources")
before any implementation work starts on `scripts/sync_netflix.py` / `scripts/sync_primevideo.py`
(currently `NotImplementedError` stubs, commit `527cbab`).

**Status: Netflix side resolved. Prime Video side still open** — see "What's still unresolved"
below.

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
- **No cookie/internal-API shortcut confirmed in this pass.** Every OSS tool found uses
  Selenium-driven login automation + screen scraping:
  - [`gitzain/prime-video-history-to-csv`](https://github.com/gitzain/prime-video-history-to-csv) —
    Python + Selenium, logs in with username/password, scrapes the history page.
  - [`twocaretcat/watch-history-exporter-for-amazon-prime-video`](https://github.com/twocaretcat/watch-history-exporter-for-amazon-prime-video) —
    browser-console script; the user must already be logged in and paste it in manually each
    time (zero automation risk, but also zero automation — doesn't meet the "no manual work"
    goal on its own).
  - This is the highest-risk tier of everything surveyed: Amazon's bot/fraud detection is tied
    to the same stack protecting its payments surface, and Selenium leaves the largest
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
- **Prime Video**: **not yet decided** — see "What's still unresolved."
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

- **Prime Video technical approach.** This pass found no cookie-replay or internal-API
  shortcut analogous to Netflix's Shakti API — only Selenium-driven login+scrape prior art,
  which is both the highest-risk-tier approach surveyed and the hardest to make incremental
  cleanly (though the history page does appear to be most-recent-first, so the watermark
  pattern should still apply once a fetch mechanism is chosen). Before committing to
  Selenium, a follow-up spike should specifically hunt for an Amazon-internal API Prime Video's
  own web client uses (analogous to how Shakti was found for Netflix) — not confirmed to exist
  or not exist here, just not yet searched for directly.
- Real per-service rate limits / device-lock thresholds aren't documented anywhere found — the
  Netflix device-lock report above is qualitative ("frequent requests"), not a number. Worth
  staying conservative (daily sync cadence, small watermark-bounded requests) rather than
  trying to find the actual threshold empirically.
