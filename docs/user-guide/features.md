# All features

The full list. The [README](../../README.md#features) has the short version.

## Features

- **Library view** — your full AniList list with star ratings, episode progress, streaming
  links, filters by format/season/tag/score, and bulk status + bulk tag updates
- **Manga/light-novel tracking** — for anime adapted from a manga or light novel, a badge
  on the library card (with an expandable detail drawer) shows whether the source
  material is still being published, its latest chapter/volume, and English licensor —
  synced weekly via AniList's own relations plus MangaDex and MangaUpdates
- **Search** — search within your own library; a separate quick-add lets you look up a
  title on AniList by name and add it straight to your list (not a full catalog browse —
  link out to AniList for that)
- **Export / Import** — download your library plus all personal-layer data (notes, tags,
  drop reasons) as a single file; the same file can be restored via import, e.g. onto a
  fresh instance
- **Personal notes** — drop reasons, custom tags, freeform notes, queue priority, and
  separate note history per rewatch, none of which AniList has a structured place for.
  A dedicated Tags page (`/tags`) renames, merges, or deletes a tag across every entry
  it's applied to in one action, rather than editing each anime's notes individually
- **Multi-user** — invite-only accounts on the same instance; local email+password login
  supports optional TOTP-based two-factor authentication, and Settings lets you view and
  revoke your own active sessions. An opt-in "also watching" indicator shows who else on
  the instance has a show in their library, with per-user hidden tags/genres and an
  anonymize-my-activity option so nothing is surfaced by default. Admins get a tabbed
  panel for invites, soft user deactivation, an audit log of admin actions, an
  instance-health readout, a data-quality page (sync drift, orphaned rows), and a
  one-click all-users backup export
- **Installable** — a Progressive Web App with a mobile-responsive layout, so it can be
  added to a phone or desktop home screen and opened like a native app
- **Collections** — save a combination of filters (status, tag, score, format) as a named
  shortcut on the library view; re-applying it always reflects your library's current
  state, since it's a saved filter, not a fixed list of shows
- **Recommendations** — unwatched/planning anime scored against the genres, tags, and
  studios of your highest-rated completed shows; dismiss with a reason, snooze for a
  while, or mark as seen (pushes COMPLETED + rating to AniList). Includes a "new this
  season" seasonal discovery digest
- **Upcoming episodes** — airing schedule for anything in your Watching/Planning list,
  plus a weekly Mon–Sun broadcast grid view
- **Queue** — watch-next list ordered by recommendation score and manual priority,
  filterable by tag and episode-count bucket, plus a separate Watching tab (ordered by
  most recently progressed) for a quick "what do I pick back up" view of what's already
  in progress
- **Stats** — watch time, completion rate, score distribution, top genres and studios, a
  watch-activity calendar heatmap, a full-screen animated "year in anime" wrap-up, and a
  drop-pattern breakdown (genres/tags/words that show up most in what you drop)
- **Crunchyroll sync** — watch history and progress synced from Crunchyroll into AniList,
  fetched directly via a cookie-authenticated client, no third-party tool (optional)
- **Netflix sync** — watch history synced from Netflix's own viewing-activity API into
  AniList progress, cookie-authenticated, incremental (optional). A CSV export import is
  also available as a one-time bootstrap fallback for accounts with a lot of history
- **Plex sync** — watch history synced from your own Plex Media Server into AniList
  progress, via Plex's real OAuth sign-in flow (no cookie to find or paste), incremental
  (optional)
- **Prime Video sync** — watch history synced from Prime Video into AniList progress,
  cookie-authenticated, incremental (optional)
- **Notifications** — new episode alerts, sync results, weekly and monthly digests, and
  airing-schedule-change alerts, delivered to any combination of Telegram, Discord
  (webhook), ntfy, and Web Push, each toggled independently (optional)
- **Multi-language UI** — English, Spanish, Hindi, Japanese, and Simplified Chinese,
  switchable per-user in Settings
