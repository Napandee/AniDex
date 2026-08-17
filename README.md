# AniDex

A self-hosted anime tracking, rating, and recommendation app built on top of AniList.

AniList handles the catalog. This app adds the personal layer AniList's own UI doesn't
give a good home to: drop reasons, custom tags, freeform notes, a real "watch next" queue,
and a recommendation engine scored against your own taste profile.

## Features

- **Library view** — your full AniList list with star ratings, episode progress, streaming
  links, filters by format/score/season, and bulk status updates
- **Search** — search within your own library; a separate quick-add lets you look up a
  title on AniList by name and add it straight to your list (not a full catalog browse —
  link out to AniList for that)
- **Export** — download your library plus all personal-layer data (notes, tags, drop
  reasons) as a single file
- **Personal notes** — drop reasons, custom tags, freeform notes, and queue priority per
  show; none of which AniList has a structured place for
- **Multi-user** — invite-only accounts on the same instance; an opt-in "also watching"
  indicator shows who else on the instance has a show in their library, with per-user
  hidden tags/genres and an anonymize-my-activity option so nothing is surfaced by
  default
- **Recommendations** — unwatched/planning anime scored against the genres, tags, and
  studios of your highest-rated completed shows; dismiss with a reason or mark as seen
  (pushes COMPLETED + rating to AniList)
- **Upcoming episodes** — airing schedule for anything in your Watching/Planning list
- **Queue** — watch-next list ordered by recommendation score and manual priority
- **Stats** — watch time, completion rate, score distribution, top genres and studios
- **Crunchyroll sync** — watch history and progress synced from Crunchyroll into AniList,
  fetched directly via a cookie-authenticated client, no third-party tool (optional)
- **Netflix sync** — watch history synced from Netflix's own viewing-activity API into
  AniList progress, cookie-authenticated, incremental (optional)
- **Telegram notifications** — new episode alerts, sync results, weekly digest (optional)

## Architecture

```
Crunchyroll ──► sync_crunchyroll.py ──┐
                                       ├──► AniList ──► sync_anilist.py ──► Postgres ──► FastAPI app ──► http://localhost:8888
Netflix ──────► sync_netflix.py ──────┘
```

The app includes a built-in scheduler (APScheduler) that runs the daily AniList sync and
weekly recommender automatically. Schedule is configurable via the Settings page.

## Prerequisites

- Docker and Docker Compose
- An AniList account
- A Postgres instance (a compose file is provided)
- Optional: Crunchyroll account, Netflix account, Telegram bot, GitHub account for CI/CD

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

It runs on ports distinct from the documented live-instance ports (8889/5433) so it
can coexist with a real deploy on the same machine. Works with `docker compose` or
`podman compose` interchangeably — swap the binary name in the commands below.

**1. Bring up the stack** (builds the app image locally, auto-applies `schema.sql` on
first boot via Postgres's init-script mount):

```bash
docker compose -f compose/dev.yml up --build
```

**2. Register the first account** — visit `http://localhost:28888/auth/register`.
Being the first user, it becomes admin automatically; no invite needed.

**3. Seed some fake library data** so there's something to test against:

```bash
docker exec -i $(docker compose -f compose/dev.yml ps -q anidex-dev-postgres) \
  psql -U anime_tracker -d anime_tracker \
  -v email="'you@example.com'" \
  -f - < scripts/dev_seed.sql
```

(Use the same email you registered with.) This inserts a handful of `anime` +
`library_entries` rows across a few statuses — enough to exercise drop/complete/status
changes, filters, etc. without touching AniList.

**4. Test in browser** at `http://localhost:28888`.

**5. Tear down** when done — `-v` also drops the throwaway Postgres volume, so nothing
lingers between test runs:

```bash
docker compose -f compose/dev.yml down -v
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
the "Sync Now" button on the Settings page (or `POST /api/sync`).

## Telegram notifications (optional)

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in Settings (or in `.env`) to enable:

- New episode alerts for anything in your Watching/Planning list
- Daily sync success/failure notification
- Weekly digest of upcoming episodes

Create a bot via [@BotFather](https://t.me/BotFather) on Telegram to get a token.
Your chat ID can be retrieved by messaging [@userinfobot](https://t.me/userinfobot).

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
these are separate from the app's own `TELEGRAM_BOT_TOKEN` in `.env` below (that one's
for in-app notifications; this one's a GitHub Actions secret for repo maintainers).

## Social login (Google/Discord, optional)

Google and Discord "Sign in with..." are optional alternatives to local email+password,
admin-configured per-instance via **Admin → OAuth settings** (or the `GOOGLE_CLIENT_ID`
/ `DISCORD_CLIENT_ID` env vars as a fallback — see the table below). This is a one-time,
instance-wide setup: the client id/secret are never seen or entered by individual users,
who just click Connect/Sign-in and authenticate with their own provider account, the
same as any "Sign in with Google" button anywhere on the web.

1. Create an OAuth app in the [Google Cloud Console](https://console.cloud.google.com/)
   and/or the [Discord Developer Portal](https://discord.com/developers/applications).
2. Register both redirect URIs for whichever provider(s) you're setting up (`{provider}`
   is `google` or `discord`), using your instance's actual public URL:
   - `https://your-instance-domain/auth/callback/{provider}`
   - `https://your-instance-domain/auth/link-callback/{provider}`
3. Paste the client id/secret into Admin → OAuth settings and save — no restart needed.

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
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | No | Google OAuth login — fallback if not set via `/admin/oauth-settings` in the app |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | No | Discord OAuth login — same fallback pattern as Google above |
| `CRUNCHYROLL_ETP_RT` | No | Crunchyroll session cookie — enables CR watch history sync |
| `NETFLIX_COOKIE_HEADER` | No | Full Netflix session cookie header — enables Netflix watch history sync |
| `NETFLIX_PROFILE_GUID` | No | Netflix profile guid — required alongside `NETFLIX_COOKIE_HEADER` |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token — enables notifications |
| `TELEGRAM_CHAT_ID` | No | Your Telegram chat ID — where notifications are sent |
| `SESSION_SECRET_KEY` | Recommended | Signs session cookies. If unset, a random key is generated per process start and sessions won't survive a container restart — set this for any real deployment |
| `GHCR_TOKEN` | CI/CD only | GitHub PAT with `read:packages` scope — used by the deploy job to pull from GHCR |
| `TZ` | No | Container timezone, e.g. `Europe/London` (default: UTC) |

All variables can also be set via the Settings page in the app after first launch.
Credentials stored in Settings are saved to the `settings` table in Postgres.

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
