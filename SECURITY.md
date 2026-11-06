# Security

## Reporting a vulnerability

Please use **[Report a vulnerability](https://github.com/Napandee/AniDex/security/advisories/new)** on this repo's Security tab, not a public issue. Include what you found and how to reproduce it.

AniDex is a one-person project, so replies may take a few days.

## Supported versions

Only the [latest release](https://github.com/Napandee/AniDex/releases/latest).

## What's in place

- Each release image is scanned with Trivy before it's published, and a scan with fixable high or critical findings stops the release.
- The container runs as an unprivileged user and only writes to `/tmp`.
- Stored AniList and streaming credentials are encrypted with `SETTINGS_ENCRYPTION_KEY`.
