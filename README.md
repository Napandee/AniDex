# AniDex

A self-hosted anime tracking, rating, and recommendation app built on top of AniList.

AniList handles the catalog. This app adds the personal layer AniList's own UI doesn't
give a good home to: drop reasons, custom tags, freeform notes, a real "watch next" queue,
and a recommendation engine scored against your own taste profile.

## Features

- **Library view** — your full AniList list with star ratings, episode progress, streaming
  links, filters by format/score/season, and bulk status updates
- **Personal notes** — drop reasons, custom tags, freeform notes, and queue priority per
  show; none of which AniList has a structured place for
- **Recommendations** — unwatched/planning anime scored against the genres, tags, and
  studios of your highest-rated completed shows; dismiss with a reason or mark as seen
  (pushes COMPLETED + rating to AniList)
- **Upcoming episodes** — airing schedule for anything in your Watching/Planning list
- **Queue** — watch-next list ordered by recommendation score and manual priority
- **Stats** — watch time, completion rate, score distribution, top genres and studios
- **Crunchyroll sync** — watch history and progress synced from Crunchyroll into AniList
  via [crunchyexporter-cli](https://github.com/ruflas/crunchyexporter-cli) (optional)
- **Telegram notifications** — new episode alerts, sync results, weekly digest (optional)

## Architecture

```
Crunchyroll ──► crunchyexporter-cli ──► sync_crunchyroll.py ──► AniList
                                                                     │
                                                              AniList GraphQL API
                                                                     │
                                                            sync_anilist.py
                                                                     │
                                                                Postgres
                                                                     │
                                                             FastAPI app
                                                                     │
                                                          http://localhost:8888
```

The app includes a built-in scheduler (APScheduler) that runs the daily AniList sync and
weekly recommender automatically. Schedule is configurable via the Settings page.

## Prerequisites

- Docker and Docker Compose
- An AniList account
- A Postgres instance (a compose file is provided)
- Optional: Crunchyroll account, Telegram bot, GitHub account for CI/CD

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

**5. Run your first sync**

```bash
docker exec anidex python scripts/sync_anilist.py
```

**6. Open the app**

Visit `http://localhost:8888`. Your library should be populated after the sync completes.

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

> **Note:** this relies on [crunchyexporter-cli](https://github.com/ruflas/crunchyexporter-cli),
> an unofficial, community-maintained tool that reads your Crunchyroll session cookie —
> it isn't affiliated with or supported by Crunchyroll, and could stop working if
> Crunchyroll changes their site. It's MIT-licensed, so there's no license concern using
> it here, but using any tool that acts on your behalf with your session credentials is
> ultimately your own call with respect to Crunchyroll's Terms of Service. This feature
> is entirely optional — skip it if you'd rather not take that on.

For how to extract your Crunchyroll session cookie (`etp_rt`) see the
[crunchyexporter-cli documentation](https://github.com/ruflas/crunchyexporter-cli).
Once you have the cookie, set `CRUNCHYROLL_ETP_RT` in your `.env` (or in Settings after
first launch).

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

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | Yes | Postgres connection string — `postgresql://user:pass@host:port/db` |
| `ANILIST_USERNAME` | Yes | Your AniList username (not email) — used to fetch your library |
| `ANILIST_TOKEN` | For writes | OAuth token — needed for rating, status, and progress updates |
| `CRUNCHYROLL_ETP_RT` | No | Crunchyroll session cookie — enables CR watch history sync |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token — enables notifications |
| `TELEGRAM_CHAT_ID` | No | Your Telegram chat ID — where notifications are sent |
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
