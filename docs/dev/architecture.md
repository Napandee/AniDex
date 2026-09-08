# Architecture

How AniDex fits together: services, data flow, key dependencies.

- **Sync job**: `scripts/run_full_sync.py`, single-user primitive — always invoked with a
  `USER_ID` env var, either by the manual "Sync Now" trigger (that user only) or the
  built-in scheduler's loop over every user with credentials configured (sequential, one
  user's failure caught and logged without blocking the rest — see `_scheduled_full_sync`
  in `app/main.py`). Chains steps: AniList→Postgres sync (`sync_anilist.py`, runs first,
  #99) → CR→AniList progress sync (`sync_crunchyroll.py`) → Netflix→AniList progress sync
  (`sync_netflix.py`) → Plex→AniList progress sync (`sync_plex.py`) → Prime Video→AniList
  progress sync (`sync_primevideo.py`, issue #17, endpoint confirmed live 2026-08-26 —
  see `notes/2026-08-14-netflix-prime-sync-research.md`) — each of the four provider
  steps independently skipped if that provider's credentials aren't configured for the
  user. `sync_crunchyroll.py`/`sync_netflix.py`/`sync_primevideo.py` fetch their
  respective service's watch history directly (cookie-authenticated API clients, no
  vendored third-party CLI, no intermediate history file); `sync_plex.py` uses a
  server-scoped `X-Plex-Token` instead (OAuth PIN connect flow, `app/plex_auth.py`) —
  all four walk newest-first, stopping at a Postgres-backed per-user watermark
  (`cr_sync_state`/`netflix_sync_state`/`plex_sync_state`/`primevideo_sync_state`'s
  `last_seen_watched_at`) so a routine sync only walks genuinely new activity.
  Crunchyroll/Plex/Prime Video track progress as an absolute episode number
  (`last_seen_episode`, parsed directly from each provider's own episode metadata —
  Prime Video's watch-history response includes an exact `"Episode N: <title>"` string
  per watched episode, confirmed live); Netflix has no absolute episode-ordinal field at
  all, so `sync_netflix.py` alone falls back to a distinct-new-episode delta count (see
  its own module docstring for why). Upserts into `anime` / `library_entries` /
  `airing_schedule_cache` / the four provider `*_sync_state` tables, all scoped to that
  user except `anime`/`airing_schedule_cache` which stay global. Progress/status pushes
  back to AniList are no longer synchronous `SaveMediaListEntry` calls made inline during
  the sync — every provider script calls `enqueue_outbox_update()`
  (`scripts/anilist_sync_common.py`) to write a `status_sync_outbox` row instead,
  delivered by the app's single shared outbox worker (`app/outbox.py`), unified with the
  UI's own bulk-edit outbox (#18) so every AniList write source is decoupled and
  rate-limited together (#100). There is no separate sync container.
  **Create-vs-skip contract for an unmatched title (issue #252):** when a provider sync
  resolves a title to a real AniList `media_id` but that anime has no existing
  `library_entries` row for the user, the decision depends on `full_pull` — whether
  this run is the very first connect's full historical walk, or a user-triggered force
  full resync (issue #20/#21), both of which set the flag the same way. `full_pull ==
  True` keeps the conservative behavior: skip, never auto-create — walking a user's
  entire history and creating dozens/hundreds of old entries would flood their real
  AniList list. `full_pull == False` (routine day-to-day incremental sync) creates a
  new entry instead of skipping, via the same `enqueue_outbox_update()` path already
  used for progress/status updates. Default status is `WATCHING` at the detected
  progress for episodic content (TV/series) — but a movie/single-sitting title has no
  sensible "still watching" resting state, so it must land `COMPLETED` at progress=1
  instead, exactly like an existing PLANNING movie entry's first-watch handling
  already does (`sync_netflix.py`'s `process()` checks its MOVIE branch before the
  brand-new-entry branch specifically so a synthetic new movie entry falls into that
  existing logic rather than needing its own copy of it — a real regression from
  applying WATCHING uniformly was caught in review before merge, see git history).
  `SaveMediaListEntry` upserts on AniList's side either way, so this is not a new
  mutation type. `scripts/anilist_sync_common.py`'s `resolve_or_create_user_list_entry()`
  is the single shared implementation of this decision (used by both
  `sync_crunchyroll.py` and `sync_netflix.py`, so the two providers can't drift), plus
  `ensure_anime_stub()` for the local `anime` row a synthetic entry's foreign keys
  require (the global `anime` table only ever gets a row for media already on
  *someone's* list — a title nobody has tracked yet has no local row to reference until
  this stub creates one). Plex (#153) and Prime Video (#17) both already implement this
  pattern, reusing `resolve_or_create_user_list_entry()` directly rather than
  reintroducing an unconditional skip. **Any future provider sync script — Jellyfin
  (#150–153) — must implement this same full_pull-gated create-vs-skip pattern from day
  one**, not ship the original bug and need this same fix retrofitted later.
  **Issue #387 hardened this after a real incident**: a partial/interrupted dev run
  against a real account left a few real `primevideo_sync_state` rows behind; the next
  real run trusted that leftover state as "walk complete" via an unsafe fallback
  (`has_existing_state` — "if sync-state rows already exist, assume the walk finished"),
  flipping `full_pull` to `False` on a still-mostly-unwalked year of history and
  auto-creating 16 bogus AniList entries in one run (14 of which were false-positive
  title matches). Two fixes: (1) `load_walk_complete()`/`set_walk_complete()` now trust
  only an explicit, persisted `{provider}_walk_complete` flag in `settings` — no row
  means "hasn't completed a walk yet," full stop, no inference (migration 034 backfills
  the flag once, explicitly, for CR/Netflix/Plex users who predated it). (2)
  `resolve_or_create_user_list_entry()` now fetches the candidate's real AniList
  metadata (`fetch_anilist_media_metadata()`) and runs a title-similarity gate
  (`_title_similarity()`, `TITLE_SIMILARITY_THRESHOLD = 0.6`, tightened to
  `UNKNOWN_METADATA_TITLE_SIMILARITY_THRESHOLD = 0.85` when AniList metadata itself
  couldn't be fetched) before ever creating an entry — a weak title match no longer
  silently becomes a real library row. Every provider script's `main()` also gained a
  `DRY_RUN` env var (all four, not just Prime Video) that exercises this same
  matching/validation path for real without ever opening a DB write connection.
- **Recommender job**: runs `run_recommender.py`, same per-user/`USER_ID` pattern as the
  sync job. Scores unwatched/planning anime against that user's taste profile, writes to
  `recommendation_scores`. Never touches the `dismissed` flag.
- **Filler data sync** (issue #299): `scripts/sync_filler_data.py`, catalog-wide like
  `sync_airing_schedule.py` — no `USER_ID`, one pass over every distinct `anime.id` in
  the local catalog rather than per-user. Looks each up against AniFillerPedia by
  `anilist_id` (direct integer match, no fuzzy matching), caching researched
  filler/canon episodes into `filler_episode_cache` and per-title check state into
  `filler_sync_state`. Runs on its own daily APScheduler job
  (`_refresh_filler_data`/`filler_data_refresh` in `app/main.py`, fixed 03:15 UTC, not
  tied to the user-configurable daily-sync time in Instance Config) — independent of
  both `run_full_sync.py` and the hourly airing-schedule refresh, since filler status
  is static catalog metadata that barely changes once approved, not per-user or
  airing-state data.
- **App**: reads all tables, scoped to the logged-in user; writes to `personal_notes`, the
  `dismissed` flag on `recommendation_scores`, and `library_entries.score` (via the rating
  endpoint). Also pushes ratings, status, and progress to AniList via `SaveMediaListEntry`
  — real-time for single-item edits, through the shared outbox above for bulk-status
  edits.
- **Built-in scheduler**: APScheduler runs inside the app container, registering 10 cron
  jobs (see the `_scheduler.add_job()` calls in `app/main.py`): `daily_sync` and
  `weekly_recommender` (per-user, admin-configurable time via Instance Config — moved
  there from Settings once Admin was split into tabs, see Epic #93), `airing_schedule_refresh`
  (hourly) and `airing_check` (hourly, feeds #374's change-alerts), `filler_data_refresh`
  (#299, fixed 03:15 UTC), `weekly_digest` (Monday mornings), `monthly_recap` (#375, 1st
  of the month, offset from `weekly_digest`'s slot), `wrapped_midyear_checkin` /
  `wrapped_year_end_reminder` (#196, two fixed calendar dates), and `instance_backup`
  (#372, admin-configurable time like the first two). `daily_sync`/`weekly_recommender`/
  `instance_backup` are the only ones with a user-configurable *time*; the rest are fixed,
  instance-wide UTC schedules chosen to avoid competing with each other.
