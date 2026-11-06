# Installing AniDex

Full install steps. The short version is in the [README](../../README.md#quick-start).

## Prerequisites

- Docker and Docker Compose
- An AniList account
- A Postgres instance, version 16+ (a compose file is provided for a dedicated bundled
  instance — or point AniDex at an existing shared Postgres instance instead, see
  [docs/admin/shared-postgres.md](shared-postgres.md))
- Optional: Crunchyroll account, Netflix account, Plex account/server, Prime Video
  account, a notification channel (Telegram bot, Discord webhook, and/or ntfy), GitHub
  account for CI/CD

## Quick start

**1. Clone the repo and set up your environment**

```bash
git clone https://github.com/Napandee/AniDex.git
cd AniDex
cp .env.example .env
```

Edit `.env` and fill in at minimum `DATABASE_URL`, `ANILIST_USERNAME` and `SETTINGS_ENCRYPTION_KEY`. The app won't start without that key. To generate one:

```bash
head -c 32 /dev/urandom | base64 | tr '+/' '-_'
```

**2. Start Postgres**

```bash
# Set a password and start
POSTGRES_PASSWORD=yourpassword docker compose -f compose/anidex-postgres.yml up -d
```

Then add the same password to your `DATABASE_URL` in `.env`.

**3. Initialise the database**

```bash
docker run --rm --network host \
  -v "$PWD/schema.sql:/schema.sql" \
  postgres:16-alpine \
  psql "$(grep ^DATABASE_URL .env | cut -d= -f2-)" -f /schema.sql
```

**4. Start the app**

```bash
docker run -d \
  --name anidex \
  --restart unless-stopped \
  --network host \
  --env-file .env \
  --read-only \
  --tmpfs /tmp \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  ghcr.io/napandee/anidex:latest
```

The image runs as an unprivileged user and only ever writes to `/tmp` (e.g. the Netflix
CSV import's tempfile) — `--read-only` plus the `/tmp` tmpfs mount above match what the
real deploy pipeline runs (see [CI/CD](../../CONTRIBUTING.md#cicd-with-github-actions-optional) below).
They're not required for the app to boot, just recommended.

Or build locally first:

```bash
docker build -t anidex:local .
docker run -d --name anidex --network host --env-file .env \
  --read-only --tmpfs /tmp --cap-drop=ALL --security-opt=no-new-privileges \
  anidex:local
```

**5. Register your account**

Visit `http://localhost:8888/auth/register`. The app is multi-user — the first account
you register on an empty database bootstraps as admin automatically, no invite needed.
Anyone after that needs an admin-issued invite (`/admin/invites`). If Google login is
set up (see [Social login](social-login.md#social-login-googlediscord-optional) below), remember to
add each new invitee's Google email as a test user in the GCP console too, or their
first Google sign-in attempt will fail.

**6. Add your AniList credentials and sync**

Set `ANILIST_USERNAME` and `ANILIST_TOKEN` on the Settings page (or in `.env` before
first boot), then click **Sync Now** — this runs the same per-user sync pipeline the
built-in scheduler uses daily. Your library should be populated once it completes.

Scripts under `scripts/` (`sync_anilist.py`, `run_full_sync.py`, etc.) are single-user
primitives invoked with a `USER_ID` env var — they're what the app and scheduler call
internally, not meant to be run manually against a container from the CLI.

## Getting your AniList token

The AniList token is needed for write operations — rating anime, updating status, and
syncing progress back to AniList. Without it the app is read-only.

1. Go to [AniList Developer Settings](https://anilist.co/settings/developer) and create
   a new API client (any name, redirect URI can be `https://anilist.co/api/v2/oauth/pin`)
2. Follow the [OAuth PIN flow](https://anilist.gitbook.io/anilist-apiv2-docs/overview/oauth/authorization-code-grant)
   to obtain an access token
3. Set `ANILIST_TOKEN` in your `.env`
