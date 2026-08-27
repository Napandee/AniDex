# AniDex

A self-hosted anime tracking, rating, and recommendation app built on top of AniList.

AniList handles the catalog. This app adds the personal layer AniList's own UI doesn't
give a good home to: drop reasons, custom tags, freeform notes, a real "watch next" queue,
and a recommendation engine scored against your own taste profile.

> **Using an already-running instance?** This README covers installing and deploying
> AniDex. For how to actually use it once it's up — collections, recommendations,
> streaming coverage, the MCP server for AI clients, and everything else — see the
> [user guide](docs/user-guide/index.md).

## Features

- **Library view** — your full AniList list with star ratings, episode progress, streaming
  links, filters by format/season/tag/score, and bulk status + bulk tag updates
- **Search** — search within your own library; a separate quick-add lets you look up a
  title on AniList by name and add it straight to your list (not a full catalog browse —
  link out to AniList for that)
- **Export / Import** — download your library plus all personal-layer data (notes, tags,
  drop reasons) as a single file; the same file can be restored via import, e.g. onto a
  fresh instance
- **Personal notes** — drop reasons, custom tags, freeform notes, queue priority, and
  separate note history per rewatch, none of which AniList has a structured place for
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
  filterable by tag and episode-count bucket
- **Stats** — watch time, completion rate, score distribution, top genres and studios, a
  watch-activity calendar heatmap, a "year in anime" wrap-up, and a drop-pattern
  breakdown (genres/tags/words that show up most in what you drop)
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
- **Notifications** — new episode alerts, sync results, and a weekly digest, delivered to
  any combination of Telegram, Discord (webhook), and ntfy, each toggled independently
  (optional)
- **Multi-language UI** — English, Spanish, Hindi, Japanese, and Simplified Chinese,
  switchable per-user in Settings

## Architecture

```
Crunchyroll ──► sync_crunchyroll.py ──┐
Netflix ──────► sync_netflix.py ───────┤
Plex ─────────► sync_plex.py ──────────┼──► AniList ──► sync_anilist.py ──► Postgres ──► FastAPI app ──► http://localhost:8888
Prime Video ──► sync_primevideo.py ────┘
```

The app includes a built-in scheduler (APScheduler) that runs the daily AniList sync and
weekly recommender automatically. Schedule is configurable via the Admin page.

Writes back to AniList (bulk status/tag edits, and any Crunchyroll/Netflix/Plex/Prime
Video-originated progress update) go through an async outbox rather than blocking the
request that triggered them — a background worker drains it and retries on transient
AniList failures, so a slow or briefly-down AniList API doesn't stall the UI.

## Prerequisites

- Docker and Docker Compose
- An AniList account
- A Postgres instance (a compose file is provided)
- Optional: Crunchyroll account, Netflix account, Plex account/server, Prime Video
  account, a notification channel (Telegram bot, Discord webhook, and/or ntfy), GitHub
  account for CI/CD

## Quick start

**1. Clone the repo and set up your environment**

```bash
git clone https://github.com/yourname/AniDex.git
cd AniDex
cp .env.example .env
```

Edit `.env` and fill in at minimum `DATABASE_URL` and `ANILIST_USERNAME`.

**2. Start Postgres**

```bash
# Set a password and start
POSTGRES_PASSWORD=yourpassword docker compose -f compose/anidex-postgres.yml up -d
```

Then add the same password to your `DATABASE_URL` in `.env`.

**3. Initialise the database**

```bash
docker run --rm --env-file .env \
  -v $(pwd)/schema.sql:/schema.sql \
  postgres:16-alpine \
  psql "$DATABASE_URL" -f /schema.sql
```

**4. Start the app**

```bash
docker run -d \
  --name anidex \
  --restart unless-stopped \
  -p 8888:8888 \
  --env-file .env \
  ghcr.io/yourname/anidex:latest
```

Or build locally first:

```bash
docker build -t anidex:local .
docker run -d --name anidex -p 8888:8888 --env-file .env anidex:local
```

**5. Register your account**

Visit `http://localhost:8888/auth/register`. The app is multi-user — the first account
you register on an empty database bootstraps as admin automatically, no invite needed.
Anyone after that needs an admin-issued invite (`/admin/invites`). If Google login is
set up (see [Social login](#social-login-googlediscord-optional) below), remember to
add each new invitee's Google email as a test user in the GCP console too, or their
first Google sign-in attempt will fail.

**6. Add your AniList credentials and sync**

Set `ANILIST_USERNAME` and `ANILIST_TOKEN` on the Settings page (or in `.env` before
first boot), then click **Sync Now** — this runs the same per-user sync pipeline the
built-in scheduler uses daily. Your library should be populated once it completes.

Scripts under `scripts/` (`sync_anilist.py`, `run_full_sync.py`, etc.) are single-user
primitives invoked with a `USER_ID` env var — they're what the app and scheduler call
internally, not meant to be run manually against a container from the CLI.

## Local development & pre-merge testing

Verifying a change against the real deployment is risky and slow — this is a second,
throwaway stack for clicking through UI/backend changes on localhost before merging,
built from your local `Dockerfile` instead of pulling from GHCR. It needs no AniList
or Crunchyroll credentials: the app only hard-requires `DATABASE_URL` to boot, and
local email+password auth means the first account you register bootstraps as admin.
The stack sets `ANILIST_MOCK=1`, which skips the live `SaveMediaListEntry` push on the
rating/status/progress endpoints so they can be exercised with no real AniList account —
never set this outside `compose/dev.yml`.

Host ports are auto-assigned rather than fixed, and `scripts/dev/up.sh` /
`scripts/dev/down.sh` always run compose with a project name derived from this
checkout's path — so multiple worktrees/sessions can each bring the stack up at the
same time without colliding on shared container names or ports. (Fixed names/ports
used to mean a second `up --build` from another worktree would silently tear down
and recreate the first one's containers, wiping its throwaway Postgres volume — this
is why raw `docker compose`/`podman compose` invocations against this file are no
longer the documented path.) Use `docker compose` instead of `podman compose` by
setting `COMPOSE_BIN="docker compose"` in the environment before running the scripts.

**1. Bring up the stack** (builds the app image locally, auto-applies `schema.sql` on
first boot via Postgres's init-script mount, prints the assigned app port):

```bash
scripts/dev/up.sh
```

**2. Register the first account** — visit `http://localhost:<printed-port>/auth/register`.
Being the first user, it becomes admin automatically; no invite needed.

**3. Seed some fake library data** so there's something to test against:

```bash
${COMPOSE_BIN:-podman compose} -p "$(cat .dev-stack-project)" -f compose/dev.yml \
  exec -T anidex-dev-postgres \
  psql -U anime_tracker -d anime_tracker \
  -v email="'you@example.com'" \
  -f - < scripts/dev_seed.sql
```

(Use the same email you registered with.) This inserts a handful of `anime` +
`library_entries` rows across a few statuses — enough to exercise drop/complete/status
changes, filters, etc. without touching AniList.

**4. Test in browser** at `http://localhost:<printed-port>`.

**5. Tear down** when done — also drops the throwaway Postgres volume, so nothing
lingers between test runs:

```bash
scripts/dev/down.sh
```

This stack is dev-only — it never touches `compose/anidex.yml` /
`compose/anidex-postgres.yml` or the real deploy pipeline.

**Running the test suite** — unit tests for the sync scripts live under `tests/` and
don't need Postgres or the dev stack running (`tests/conftest.py` stubs the env vars
those scripts read at import time):

```bash
pip install -r requirements-dev.txt
pytest
```

Note CI (`pr-validate.yml`) doesn't run this suite — it builds the image and smoke-tests
one rendered route instead. Run `pytest` locally before opening a PR that touches
`scripts/*.py`.

## Getting your AniList token

The AniList token is needed for write operations — rating anime, updating status, and
syncing progress back to AniList. Without it the app is read-only.

1. Go to [AniList Developer Settings](https://anilist.co/settings/developer) and create
   a new API client (any name, redirect URI can be `https://anilist.co/api/v2/oauth/pin`)
2. Follow the [OAuth PIN flow](https://anilist.gitbook.io/anilist-apiv2-docs/overview/oauth/authorization-code-grant)
   to obtain an access token
3. Set `ANILIST_TOKEN` in your `.env`

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

See the [sync providers guide](docs/user-guide/sync-providers.md) for what happens to
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
the [sync providers guide](docs/user-guide/sync-providers.md).

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

## Notifications (optional)

Configured per-user under **Settings → Notifications**. Three channels are supported,
each with its own on/off toggle so you can run any combination of them:

- New episode alerts for anything in your Watching/Planning list
- Daily sync success/failure notification
- Weekly digest of upcoming episodes

**Telegram** — set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (in Settings, or in `.env`
before first boot). Create a bot via [@BotFather](https://t.me/BotFather) to get a token;
your chat ID can be retrieved by messaging [@userinfobot](https://t.me/userinfobot).

**Discord** — paste a channel webhook URL into Settings. Create one via a Discord
channel's *Edit Channel → Integrations → Webhooks*. Discord/ntfy have no `.env` fallback —
they're Settings-only, since they're free-text URLs rather than a fixed provider host.

**ntfy** — set a topic (and optionally a non-default server URL and auth token if
self-hosting ntfy) in Settings. Uses [ntfy.sh](https://ntfy.sh) by default; no signup
needed, just pick an unguessable topic name and subscribe to it in the ntfy app.

See the [notifications guide](docs/user-guide/notifications.md) for more.

## Home Assistant integration (optional)

`GET /api/ha/status` returns a single combined JSON payload — sync health, watch-next
queue length/title, and episodes airing today/this week — meant to be polled from Home
Assistant's built-in RESTful sensor integration rather than only viewed inside AniDex.

Authenticate with a personal access token (**Settings → Personal access tokens** — the
same tokens issued for the MCP server; read scope is enough, this endpoint never
writes). Example `configuration.yaml`:

```yaml
rest:
  - resource: https://your-anidex-host/api/ha/status
    headers:
      Authorization: !secret anidex_pat
    scan_interval: 900
    sensor:
      - name: "AniDex Sync Status"
        value_template: "{{ value_json.sync.last_result }}"
      - name: "AniDex Queue Length"
        value_template: "{{ value_json.queue.length }}"
        unit_of_measurement: "shows"
      - name: "AniDex Next Up"
        value_template: "{{ value_json.queue.next_up }}"
      - name: "AniDex Episodes Airing Today"
        value_template: "{{ value_json.airing.today }}"
        unit_of_measurement: "episodes"
```

Store the raw `adx_pat_...` token in `secrets.yaml` as `anidex_pat: "Bearer adx_pat_..."`
— the `Authorization` header needs the `Bearer ` prefix included in the secret value
itself, since `!secret` substitutes the whole header value verbatim.

See the [Home Assistant guide](docs/user-guide/home-assistant.md) for the full setup
walkthrough, and [the MCP server doc](docs/mcp.md) — personal access tokens are shared
between this integration and MCP clients.

## MCP server (optional)

AniDex exposes an [MCP](https://modelcontextprotocol.io) server at `/mcp`, running
inside the same app process, so an AI client (Claude Code, or any other MCP-compatible
client) can read — and, with a `read_write`-scoped token, write — your own library,
notes, stats, and recommendation data. Auth uses the same personal access tokens as the
Home Assistant integration above (**Settings → Personal access tokens**).

See [docs/mcp.md](docs/mcp.md) for the full tool list, scopes, and example client
config.

## CI/CD with GitHub Actions (optional)

The included workflows build and push Docker images to GHCR on every push to `main`, and
deploy to a self-hosted runner automatically.

To use them in your own fork:

1. Fork the repo
2. Go to **Settings → Actions → Variables** and add:
   - `APPDATA_PATH` — the directory on your host where `.env` lives (e.g. `/opt/anidex`)
3. Add a [self-hosted GitHub Actions runner](https://docs.github.com/en/actions/hosting-your-own-runners)
   on your server with the labels `self-hosted` and `unraid` (or edit the workflow to
   match your own labels)
4. Push to `main` — the build job runs on GitHub's hosted runners, the deploy job runs on
   yours

The `GHCR_TOKEN` referenced in the deploy job should be a GitHub PAT with
`read:packages` scope, stored in your `APPDATA_PATH/.env` file.

Two more pieces round out the pipeline:

- **PR validation** (`pr-validate.yml`) — builds (but doesn't push or deploy) on every
  pull request, so a broken dependency bump fails its check before you ever merge it.
- **Dependabot** (`dependabot.yml`) — proposes weekly version bumps for `requirements.txt`
  and the base images. Postgres major-version bumps are deliberately excluded — that
  needs a real migration, not just a new tag, so it's left as a manual decision.

Optionally, `notify-dependabot.yml` pings a Telegram bot whenever Dependabot opens a PR.
To enable it, add two repo secrets: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` — note
these are separate from the app's own `TELEGRAM_BOT_TOKEN` in `.env` (see
[Notifications](#notifications-optional) above — that one's for in-app notifications;
this one's a GitHub Actions secret for repo maintainers).

## Upgrading

Deploying new code is automatic (see CI/CD above) — applying a database schema change
is not. `migrations/` holds numbered SQL files for upgrading an already-running
instance; nothing in the deploy pipeline applies them, they're a deliberate manual
step run against your live Postgres, and **Admin → Instance Health** warns you when
one is pending. Full details: [docs/admin/upgrading.md](docs/admin/upgrading.md).

## Social login (Google/Discord, optional)

Google and Discord "Sign in with..." are optional alternatives to local email+password,
admin-configured per-instance via **Admin → Instance Config → OAuth settings** (or the
`GOOGLE_CLIENT_ID` / `DISCORD_CLIENT_ID` env vars as a fallback — see the table below).
This is a one-time,
instance-wide setup: the client id/secret are never seen or entered by individual users,
who just click Connect/Sign-in and authenticate with their own provider account, the
same as any "Sign in with Google" button anywhere on the web.

1. Create an OAuth app in the [Google Cloud Console](https://console.cloud.google.com/)
   and/or the [Discord Developer Portal](https://discord.com/developers/applications).
2. Register both redirect URIs for whichever provider(s) you're setting up (`{provider}`
   is `google` or `discord`), using your instance's actual public URL:
   - `https://your-instance-domain/auth/callback/{provider}`
   - `https://your-instance-domain/auth/link-callback/{provider}`
3. Paste the client id/secret into Admin → Instance Config → OAuth settings and save —
   no restart needed.

**Google-specific note:** unless you complete Google's app-verification review (not
needed for a small invite-only instance — reviewable, higher-friction, and pointless
overhead if you're never going to have the general public sign in), the OAuth app stays
in Google's **Testing** publishing status. In that status Google restricts sign-in to
accounts you've explicitly added as a **test user** (Google Cloud Console → OAuth
consent screen → Audience → Test users, cap 100) — so when you invite a new user who
wants to use Google login, add their Google email there first, or they'll hit an
"app not verified" block on their first attempt. This is normal, expected behavior for
any app that isn't publicly verified, not a bug — it's the tradeoff for skipping
Google's review process. Discord has no equivalent gate for the `identify`/`email`
scopes this app requests; any Discord account can connect immediately, no admin
pre-approval step needed.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | Yes | Postgres connection string — `postgresql://user:pass@host:port/db` |
| `ANILIST_USERNAME` | Yes | Your AniList username (not email) — used to fetch your library |
| `ANILIST_TOKEN` | For writes | OAuth token — needed for rating, status, and progress updates |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | No | Google OAuth login — fallback if not set via Admin → Instance Config → OAuth settings in the app |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | No | Discord OAuth login — same fallback pattern as Google above |
| `CRUNCHYROLL_ETP_RT` | No | Crunchyroll session cookie — enables CR watch history sync |
| `NETFLIX_COOKIE_HEADER` | No | Full Netflix session cookie header — enables Netflix watch history sync |
| `NETFLIX_PROFILE_GUID` | No | Netflix profile guid — required alongside `NETFLIX_COOKIE_HEADER` |
| `PLEX_SERVER_TOKEN` / `PLEX_SERVER_BASE_URL` | No | Plex server token/URL — normally set automatically by the Connect flow in Settings, not hand-entered |
| `PRIMEVIDEO_COOKIE_HEADER` | No | Prime Video session cookie — enables Prime Video watch history sync |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token — enables Telegram notifications |
| `TELEGRAM_CHAT_ID` | No | Your Telegram chat ID — where Telegram notifications are sent |
| `SESSION_SECRET_KEY` | Recommended | Signs session cookies. If unset, a random key is generated per process start and sessions won't survive a container restart — set this for any real deployment |
| `GHCR_TOKEN` | CI/CD only | GitHub PAT with `read:packages` scope — used by the deploy job to pull from GHCR |
| `TZ` | No | Container timezone, e.g. `Europe/London` (default: UTC) |

Discord webhook and ntfy notification settings are Settings-only (no `.env` equivalent —
see [Notifications](#notifications-optional) above), since they're free-text
URLs/topics rather than a single fixed provider host like Telegram's API.

All other variables above can also be set via the Settings page in the app after first
launch. Credentials stored in Settings are saved to the `settings` table in Postgres.

## Stack

| Layer | Tech |
|---|---|
| App | Python + FastAPI + Jinja2, server-rendered HTML, no build step |
| Database | Postgres 16 |
| Container | Docker |
| Scheduler | APScheduler (built into the app container) |
| CI/CD | GitHub Actions → GHCR → self-hosted runner |

## License

GPL-3.0 — see [LICENSE](LICENSE). If you fork or redistribute a modified version,
the license requires keeping the source open under the same terms.
