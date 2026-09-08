# Scope

Full in-scope / out-of-scope detail. The binding summary is in `CLAUDE.md`.

**In scope:**
- Read-only display of AniList library (status, score-as-stars, progress, cover art)
- Where-to-watch links pulled from AniList's `externalLinks` / `streamingEpisodes`
- Personal notes layer: drop reasons, custom tags, freeform notes, manual queue priority
- "Watch next" queue driven by the recommender job's output
- Upcoming-episode view for anything in Watching/Planning status
- Built-in stats page (watch time, completion rate, score distribution, top genres)
- Multi-user: local email+password auth by default, Google/Discord OAuth optional
  per-instance, invite-only signup, admin-managed. Optional TOTP-based 2FA for local
  accounts (issue #83) and a server-side session store with a Settings view/revoke
  active-sessions list (issue #82, `app/sessions.py`). See Decisions Made.
- Progressive Web App installability + a mobile-responsive pass (issue #12) — the app
  can be installed to a device home screen; `app/static/manifest.json` and
  `service-worker.js`.
- Collections: named, per-user saved filter combinations over the existing
  status/tag/score/format filters (issue #200) — a collection stores filter criteria
  only, never a list of anime ids, so it stays live against the library rather than
  going stale.
- Cross-user "also watching" indicator (opt-in per-user hidden tags/genres and
  anonymized-activity controls; nothing surfaced by default) — see `app/privacy.py`.
  Static/on-demand only; shipped as issue #29. Collaborative-filtering
  recommendations (weighting candidates by other users' ratings) shipped as
  issue #31 — see `run_recommender.py`'s `fetch_cross_user_signal`/
  `CROSS_USER_WEIGHT`. Issue #16 (rolling activity-feed banner, cross-user
  episode comments) builds further on this and is still open, gated on an
  explicit scope decision — not in scope yet.
- In-library search, plus a quick-add-by-title lookup against AniList (not a full catalog
  browse UI — see Out of scope below)
- Streaming Coverage (issue #182, milestone/tracking issue #22): per-user "services I
  own" input (`user_streaming_services`, edited from Settings), scored against
  Watching/Planning `library_entries` by episodes-remaining as marginal-value
  coverage ("adding service X would unlock N more episodes") on its own `/streaming`
  page, with a small summary card cross-linked from `/stats`. Region-aware
  availability, a "cancel candidates" inverted framing, household aggregate view, and
  set-cover framing were all considered in #22's brainstorm and deliberately deferred
  past v1 — see #22 for the full list.
- Home Assistant integration (issue #336): `GET /api/ha/status`, a single combined
  read-only JSON payload (sync health, watch-next queue length/title, episodes airing
  today/this week) meant for polling from HA's own RESTful `sensor:` integration, not
  a bespoke HA add-on/HACS integration. PAT-authenticated only (`_require_pat_user` in
  `app/main.py`, reusing `app/pat.py`'s `resolve_token` — the same primitive
  `mcp_server.py` uses) — no session-cookie fallback, since the caller is a headless
  poller. See README's "Home Assistant integration" section for the `sensor:` YAML.
- Personal library health (issue #337): a "Library health" card on Settings > Sync,
  the non-admin companion to #202's admin Data Quality tab. Reuses
  `_data_quality_signals()` unchanged, just with its new optional `user_id` filter
  set to the viewing user — same detection logic (orphaned `personal_notes`, stale
  `recommendation_scores`, AniList drift, recent sync failure rate), scoped to that
  one user's own rows instead of every user. `user_id=None` (the admin page's call)
  is unaffected.
- "On this day" (issue #338): a small card on `/upcoming`'s default view only —
  watch-start/finish anniversaries landing on today's calendar month/day, computed
  purely from `library_entries.start_date`/`finish_date` (no new stored state, same
  "compute on the fly" precedent as #164's pace-stat). Exact calendar-day match
  only, not a "this week" window; `years_ago == 0` (today, current year) is
  filtered out — that's not an anniversary of anything. See
  `_on_this_day_anniversaries()` in `app/main.py`.
- Scheduled automatic backups (issue #372): admin-configurable time (same
  Instance Config form/`instance_config` keys pattern as daily sync/weekly
  recommender), writes a full-instance export into `instance_backups`, pruning
  down to a retention count. See `_scheduled_instance_backup()`.
- Franchise/watch-order view (issue #373): the anime notes page's "related"
  section upgraded from a plain "in your library" boolean to actual per-related-
  title watch status/progress, so a franchise's prequels/sequels show where you
  actually stand on each, not just whether it's tracked at all.
- Airing-schedule-change alerts (issue #374): notifies when a tracked
  Watching/Planning title's next-episode air date shifts by more than a
  meaningful threshold, not on every minor cache refresh — see the threshold
  comment above `_check_airing_episodes()`.
- Monthly recap digest (issue #375): a monthly notification (1st of the month,
  offset from the existing weekly digest's Monday slot so the two don't compete)
  summarizing the prior month's watch activity — see
  `_compute_monthly_recap()`/`_scheduled_monthly_recap()`.
- Tag management page (issue #376): `/tags` — rename, merge, or delete a
  personal tag across every entry that carries it in one action, rather than
  editing each anime's notes individually.
- Web Push notifications (issue #377): a fourth notification channel alongside
  Telegram/Discord/ntfy, opted into via a browser permission grant (installed
  PWA or browser tab) rather than a Settings-form toggle — see
  `app/notify.py`'s `WebPushChannel` and the `push_subscriptions` table.
- AniList rate-limit/backoff visibility (issue #381): Admin → Instance Health
  surfaces the last observed AniList rate-limit signal (source, retry-after,
  when) from `anilist_rate_limit_state`, so a sync slowdown is visible instead
  of silently retrying in the background — see `_anilist_rate_limit_status()`.

**Out of scope — do not build these:**
- No re-scraping Crunchyroll directly — AniList is the only data source this app talks to
- No rebuilding AniList's catalog search/browse UI — link out to AniList for that
- No payment or public sharing features — invite-only multi-user is the ceiling here,
  not a social/sharing platform
