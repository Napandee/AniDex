# Anime Tracker — Project Context for Claude Code

## Purpose

A personal anime tracking, rating, and recommendation site. AniList is the system of
record for catalog data and list status; this app adds the personal layer AniList's own
UI doesn't support well (drop reasons, custom tags, a real "watch next" queue) and pulls
it all into one page hosted on Andreas's own infrastructure.

## Scope

**In scope:**
- Read-only display of AniList library (status, score-as-stars, progress, cover art)
- Where-to-watch links pulled from AniList's `externalLinks` / `streamingEpisodes`
- Personal notes layer: drop reasons, custom tags, freeform notes, manual queue priority
- "Watch next" queue driven by the recommender job's output
- Upcoming-episode view for anything in Watching/Planning status
- Stats view — embedded Grafana panel (`anime-tracker-stats` dashboard at `grafana.***REDACTED-DOMAIN***`)

**Out of scope — do not build these:**
- No user auth / login system — Cloudflare Access handles who can reach the site
- No re-scraping Crunchyroll directly — AniList is the only data source this app talks to
- No rebuilding AniList's catalog search/browse UI — link out to AniList for that
- No payment, sharing, or multi-user features — this is single-user

## Data Source

AniList GraphQL API — `https://graphql.anilist.co`, POST requests, no auth needed for
public reads, OAuth token needed for anything under the user's own list (already obtained
during the CrunchyExporter backfill). Rate limit: 90 req/min on the free tier — batch
queries, don't loop one-anime-per-request where a paginated query works.

Streaming links (`externalLinks`, `streamingEpisodes`) are community-curated on AniList's
side and can lag real availability — show a "last synced" timestamp next to them rather
than presenting them as guaranteed-current.

## Data Model

See `schema.sql` in repo root. Two categories, kept in separate tables on purpose:
- **AniList-sourced** (`anime`, `library_entries`, `airing_schedule_cache`) — fully
  rebuildable by the sync job. Never hand-edit rows in these tables.
- **Personal layer** (`personal_notes`, `recommendation_scores`) — the actual reason this
  app exists. Sync jobs must never write to `personal_notes`; `recommendation_scores` is
  rebuilt by the recommender job but must preserve the `dismissed` flag across rebuilds.

## Architecture

- **Sync job**: n8n workflow "Anime Tracker — Daily Sync" (04:30 daily) runs the
  `anime-tracker-crunchysync` container, which chains three steps: Crunchyroll history
  fetch via crunchyexporter-cli → CR→AniList progress sync (`sync_crunchyroll.py`) →
  AniList→Postgres sync (`sync_anilist.py`). Upserts into `anime` / `library_entries` /
  `airing_schedule_cache` / `cr_sync_state`.
- **Recommender job**: n8n workflow "Anime Tracker — Weekly Recommender" (Sundays 05:00)
  runs the same `anime-tracker-crunchysync` image with `--entrypoint python` to invoke
  `run_recommender.py`. Scores unwatched/planning anime against taste profile, writes to
  `recommendation_scores`. Never touches the `dismissed` flag.
- **App**: reads all tables; writes to `personal_notes`, the `dismissed` flag on
  `recommendation_scores`, and `library_entries.score` (via the rating endpoint).
  Also pushes ratings to AniList via `SaveMediaListEntry` mutation in real-time.

## Deploy Target

Reuses the existing GitHub → n8n → Unraid pipeline (see `unraid-config` repo for the
reference implementation):
1. Push to `main` → webhook fires to `n8n.***REDACTED-DOMAIN***`
2. n8n validates HMAC signature, SSHs to Unraid, runs the deploy script
3. Deploy script: git pull → `docker build` → `docker run` (single container, no Compose) → matches Unraid's native Docker UI pattern

Container name: `anime-tracker`
Appdata path: `***REDACTED-PATH***/anime-tracker/`
Env file: `***REDACTED-PATH***/anime-tracker/.env`
Port: `8889` (internal `8888`)
Webhook path: `anime-tracker-deploy`

Public hostname: `anime.***REDACTED-DOMAIN***` — Cloudflare Access-gated to Andreas only.

## Guardrails — Non-Negotiable

- Never commit secrets, tokens, or API keys. Env vars only, sourced from Vaultwarden
  manually — never hardcoded, never logged.
- Never write to or modify the `unraid-config` repo from this project.
- The app writes to AniList only via the rating endpoint (`POST /api/anime/{id}/rating`,
  `SaveMediaListEntry` mutation, score only). All other AniList writes go through the
  crunchysync job. Never add further AniList mutations to the app without agreement.
- Ask before any schema migration that could drop or alter existing columns/data —
  additive migrations (new nullable column, new table) are fine to just do.
- Ask before changing the deploy pipeline itself (webhook, HMAC validation, restart
  script) — this is shared plumbing with other services.
- Never use `docker-compose` or `docker compose` — deploy as a single container via
  `Dockerfile` + `docker run`, matching Unraid's native Docker UI. No `compose.yml` or
  `docker-compose.yml` should exist in this repo.

## Decisions Made

- **Repo**: `Napandee/anime-tracker` (private)
- **Tech stack**: Python + FastAPI + Jinja2 + psycopg2 — server-rendered HTML, no
  frontend build step. Uvicorn inside a slim Python Docker image.
- **Stats view**: embedded Grafana panel (`anime-tracker-stats` dashboard) — no stats
  page or stats API in the app itself.
- **Container + infra**: see Deploy Target section above.
