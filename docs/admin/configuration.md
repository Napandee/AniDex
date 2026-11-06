# Configuration

Every environment variable AniDex reads.

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
see [Notifications](integrations.md#notifications-optional) above), since they're free-text
URLs/topics rather than a single fixed provider host like Telegram's API.

All other variables above can also be set via the Settings page in the app after first
launch. Credentials stored in Settings are saved to the `settings` table in Postgres.
