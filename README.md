# anime-tracker

A personal anime tracking, rating, and recommendation site — built on top of AniList's
catalog data, with a personal layer AniList's own UI doesn't give a good home to.

## Why this exists

Crunchyroll has no export or history API, and AniList's UI doesn't track *why* something
got dropped after two episodes, or give a real "watch next" queue driven by your own
taste. This project fills that gap:

- **Watch history backfill**: [CrunchyExporter](https://github.com/ruflas/CrunchyExporter)
  pulls full Crunchyroll history and syncs it into AniList (real dates, progress, status)
- **AniList as system of record**: catalog data, list status, scores, streaming links —
  all pulled from AniList's public GraphQL API, never re-scraped from Crunchyroll directly
- **A personal layer on top**: drop reasons, custom tags, freeform notes, and a manual
  queue-priority override — none of which AniList has a structured place for
- **A recommender**: scores unwatched/planning anime against the genres, tags, and
  studios of your highest-rated completed shows
- **Upcoming-episode tracking**: airing schedule for anything in Watching/Planning status

## Status

Early planning — schema and requirements sketched, build not yet started.

## Repo contents

| File | Purpose |
|---|---|
| [`CLAUDE.md`](./CLAUDE.md) | Project scope, architecture, guardrails, and open decisions for agentic development with Claude Code |
| [`schema.sql`](./schema.sql) | Postgres schema — AniList-sourced tables kept separate from the personal-layer tables |

## Architecture (planned)

- **Data source**: AniList GraphQL API (`https://graphql.anilist.co`)
- **Sync job**: scheduled n8n workflow, upserts AniList list + airing schedule into Postgres
- **Recommender**: separate job scoring candidates against top-rated completed shows
- **App**: reads AniList-sourced + personal-layer tables; writes only to the personal layer
- **Deploy**: reuses the existing `unraid-config` GitHub → n8n → Unraid webhook pipeline —
  push to `main` triggers a build and restart on the homelab server
- **Hosting**: self-hosted on Unraid, exposed via Cloudflare Tunnel + Access
  (proposed: `anime.***REDACTED-DOMAIN***`, access-gated to a single user)

See `CLAUDE.md` for the full scope, non-goals, and guardrails before making changes.

## Open decisions

Tracked in `CLAUDE.md` — tech stack, container name/appdata path, and final hostname are
not yet settled.
