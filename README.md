<div align="center">

<img src="app/static/favicon.svg" alt="AniDex logo" width="96" height="96">

# AniDex

*Your anime tracker to watch them all.*

A self-hosted anime tracker, built on AniList, with your own notes, tags and recommendations.

[![CI](https://github.com/Napandee/AniDex/actions/workflows/build-app.yml/badge.svg?branch=main)](https://github.com/Napandee/AniDex/actions/workflows/build-app.yml)
[![Release](https://img.shields.io/github/v/release/Napandee/AniDex)](https://github.com/Napandee/AniDex/releases/latest)
[![Live demo](https://img.shields.io/badge/live%20demo-GitHub%20Pages-blue)](https://napandee.github.io/AniDex/)
[![Licence: GPL-3.0](https://img.shields.io/badge/licence-GPL--3.0-blue)](LICENSE)
![Status: stable](https://img.shields.io/badge/status-stable-brightgreen)

[Live demo](https://napandee.github.io/AniDex/) · [Quick start](#quick-start) · [Features](#features) · [Docs](docs/user-guide/index.md)

[![AniDex library view](https://github.com/Napandee/AniDex/releases/latest/download/anidex-hero.jpg)](https://napandee.github.io/AniDex/)

**Try it before you install it:** the live demo is the real interface on made-up data. Nothing you do there is saved.

</div>

## Why AniDex

AniList handles the catalogue. AniDex adds what its own pages have no good home for: drop reasons, custom tags, notes, a real watch-next queue, and recommendations scored against your own taste. Your status, progress and ratings stay in sync with AniList, and everything else stays on your own server.

## Screenshots

<table>
<tr>
<td width="70%" valign="top"><img src="https://github.com/Napandee/AniDex/releases/latest/download/anidex-desktop.jpg" alt="AniDex on the desktop"></td>
<td width="30%" valign="top"><img src="https://github.com/Napandee/AniDex/releases/latest/download/anidex-mobile.jpg" alt="AniDex on a phone"></td>
</tr>
</table>

## Features

<table>
<tr>
<td width="50%" valign="top">

**[Library](docs/user-guide/library-and-search.md)** - your AniList list with ratings, progress, filters and bulk edits.

**[Personal notes](docs/user-guide/personal-notes.md)** - drop reasons, tags, notes and rewatch history.

**[Queue and upcoming](docs/user-guide/queue-and-upcoming.md)** - what to watch next, and what airs this week.

**[Recommendations](docs/user-guide/recommendations.md)** - unwatched anime scored against your favourites.

**[Stats](docs/user-guide/stats.md)** - watch time, heatmaps, drop patterns and a [year in anime](docs/user-guide/year-in-anime.md).

**[Manga tracking](docs/user-guide/manga-tracking.md)** - is the source material still being published?

</td>
<td width="50%" valign="top">

**[Streaming sync](docs/user-guide/sync-providers.md)** - progress from Crunchyroll, Netflix, Plex and Prime Video.

**[Notifications](docs/user-guide/notifications.md)** - Telegram, Discord, ntfy and Web Push.

**[Multi-user](docs/user-guide/multi-user.md)** - invite-only accounts, two-factor login and an admin panel.

**[Home Assistant](docs/user-guide/home-assistant.md)** and **[MCP](docs/mcp.md)** - use your library from your home or an AI client.

**Installable** - a phone-friendly web app, in five languages.

**Yours to keep** - export and import your whole library.

</td>
</tr>
</table>

[All features](docs/user-guide/features.md)

## Quick start

You need Docker and an [AniList account](docs/admin/install.md#getting-your-anilist-token).

```bash
git clone https://github.com/Napandee/AniDex.git && cd AniDex
cp .env.example .env
sed -i "s|^SETTINGS_ENCRYPTION_KEY=.*|SETTINGS_ENCRYPTION_KEY=$(head -c 32 /dev/urandom | base64 | tr '+/' '-_')|" .env
POSTGRES_PASSWORD=changeme docker compose -f compose/anidex-postgres.yml up -d
docker run --rm --network host -v "$PWD/schema.sql:/schema.sql" postgres:16-alpine \
  psql "$(grep ^DATABASE_URL .env | cut -d= -f2-)" -f /schema.sql
docker run -d --name anidex --restart unless-stopped --network host --env-file .env \
  --read-only --tmpfs /tmp --cap-drop=ALL --security-opt=no-new-privileges \
  ghcr.io/napandee/anidex:latest
```

This is for a quick try on Linux; use a real password in `.env` and the compose file for anything longer. Then open `http://localhost:8888/auth/register`. The first account becomes the admin. Add your AniList token in Settings and click **Sync Now**.

[Full install guide](docs/admin/install.md)

## Documentation

- **[User guide](docs/user-guide/index.md)** - using AniDex.
- **Running it** - [install](docs/admin/install.md), [configuration](docs/admin/configuration.md), [sync setup](docs/admin/sync-setup.md), [integrations](docs/admin/integrations.md), [social login](docs/admin/social-login.md), [upgrading](docs/admin/upgrading.md), [shared Postgres](docs/admin/shared-postgres.md).
- **[Contributing](CONTRIBUTING.md)** - how it's built, local development and releases.
- **[Security](SECURITY.md)** - reporting a vulnerability.

## Licence

GPL-3.0, see [LICENSE](LICENSE). If you redistribute a modified version, its source must stay open under the same terms.
