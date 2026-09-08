# Data Model (developer)

Schema-level detail. The user-facing overview is `docs/data-model.md`.

See `schema.sql` in repo root. Four categories, kept in separate tables on purpose:
- **AniList-sourced** (`anime`, `library_entries`, `airing_schedule_cache`,
  `anilist_title_search_cache`) — fully rebuildable by the sync job. Never hand-edit rows
  in these tables.
- **Personal layer** (`personal_notes`, `rewatch_notes`, `recommendation_scores`,
  `user_streaming_services`, `collections`) — the actual reason this app exists. Sync
  jobs must never write to `personal_notes` or `rewatch_notes`; `recommendation_scores`
  is rebuilt by the recommender job but must preserve the `dismissed` flag across
  rebuilds. `user_streaming_services` (issue #182) is a per-user "services I own" set,
  edited from Settings and scored against Watching/Planning `library_entries` by
  episodes-remaining on the `/streaming` page — `service` is free TEXT validated
  against `app/main.py`'s `STREAMING_SITES` allowlist in application code (same
  allowlist that already filters `anime.external_links`), not a DB-level CHECK/FK.
  `collections` (issue #200) stores a name plus a JSON filter-criteria blob, never
  anime ids — applying a collection just re-runs the library filter it saved.
- **Auth/instance** (`users`, `invites`, `instance_config`, `password_resets`,
  `notified_episodes`, `admin_audit_log`, `sessions`, `totp_recovery_codes`) — added
  for multi-user (Aug 2026). Neither AniList-sourced nor personal-layer; sync jobs
  never touch these either. `library_entries` / `personal_notes` /
  `recommendation_scores` / `cr_sync_state` / `netflix_sync_state` / `sync_log` /
  `settings` / `notified_episodes` / `status_sync_outbox` / `user_streaming_services` /
  `collections` all carry a `user_id` scoping every row to one account.
  `admin_audit_log` is instance-wide, not per-user — it records which admin took an
  action, not whose data it affected. `sessions` (issue #82) is the server-side session
  store layered under Starlette's `SessionMiddleware`: the session cookie only ever
  carries an opaque `{"sid": ...}`, and this table is the sole place that resolves a
  sid to a `user_id`, enabling Settings' view/revoke-active-sessions list.
  `totp_recovery_codes` (issue #83) holds one-time hashed recovery codes per user for
  optional TOTP 2FA on local accounts; the TOTP secret itself lives on `users.totp_secret`.
- **External-derived cache** (`filler_episode_cache`, `filler_sync_state`,
  `filler_data_license`) — issue #299. Sourced from AniFillerPedia
  (github.com/Napandee/AniFillerPedia, a separate first-party project keying its own
  `series` table by `anilist_id`, the same id `anime.id` already is), not AniList
  itself, so kept as its own category rather than folded into "AniList-sourced" above
  — but same shape: global/catalog-wide (not per-user), fully rebuildable, never
  hand-edited, populated only by `scripts/sync_filler_data.py`. `filler_sync_state`
  tracks per-anime `last_checked_at` (and whether a match was found) so the sync
  doesn't re-query an unmatched or already-checked title on every run.
  `filler_data_license` is a single-row cache of AniFillerPedia's `/license` response
  (CC BY-NC-SA attribution) for a future UI to render without a live call. Foundation
  for three still-open UI issues (#300/#301/#302) that read this data — no UI ships
  in #299 itself.
