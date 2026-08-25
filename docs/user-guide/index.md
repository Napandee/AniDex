# AniDex user guide

AniDex is a personal anime tracking, rating, and recommendation site built on top of
your AniList library. AniList stays the system of record for catalog data and list
status — this app adds the personal layer AniList's own UI doesn't support well, and
pulls it into one page.

For installing and running an instance, see the main [README](../../README.md).
This guide covers using an already-running instance.

## Pages

- [Library and search](library-and-search.md) — your AniList library, filters, quick-add
- [Personal notes](personal-notes.md) — drop reasons, tags, mood, rewatch notes
- [Collections](collections.md) — saved filter shortcuts
- [Recommendations](recommendations.md) — how scoring works, dismiss/snooze/mark-seen
- [Queue and upcoming](queue-and-upcoming.md) — watch-next queue, airing schedule
- [Stats](stats.md) — watch time, heatmaps, year-in-anime, drop patterns
- [Streaming coverage](streaming-coverage.md) — which service unlocks the most next-up episodes
- [Multi-user and admin](multi-user.md) — invites, 2FA, sessions, admin panel
- [Sync providers](sync-providers.md) — Crunchyroll and Netflix progress sync
- [Notifications](notifications.md) — Telegram, Discord, ntfy alerts
- [Home Assistant integration](home-assistant.md) — the `/api/ha/status` sensor
- [MCP server](../mcp.md) — connecting an AI client (Claude Code, etc.) to your own library
- [Data model](../data-model.md) — how your data is organized, for the curious

## The short version

AniList is read/write for status, progress, and rating — those three fields round-trip
back to AniList the moment you change them here. Everything else (drop reasons, custom
tags, notes, queue priority, collections) lives only in AniDex, because AniList has no
structured place for it.
