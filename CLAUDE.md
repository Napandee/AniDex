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
- Stats view (genre/studio breakdown, episodes per month) — can be a page here or an
  embedded Grafana panel; decide during build, don't duplicate whichever isn't chosen

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

- **Sync job**: scheduled n8n workflow (existing pattern), pulls AniList list + airing
  schedule, upserts into `anime` / `library_entries` / `airing_schedule_cache`.
- **Recommender job**: separate job (n8n or a small script), scores unwatched/planning
  anime against genres/tags/studios of highest-rated completed entries, writes to
  `recommendation_scores`.
- **App**: reads all tables, writes only to `personal_notes` and the `dismissed` flag on
  `recommendation_scores`. No direct writes to AniList-sourced tables from the app.

## Deploy Target

Reuses the existing GitHub → n8n → Unraid pipeline (see `unraid-config` repo for the
reference implementation):
1. Push to `main` → webhook fires to `n8n.***REDACTED-DOMAIN***`
2. n8n validates HMAC signature, SSHs to Unraid, runs the deploy script
3. Deploy script: git pull → `docker build` → `docker run` (single container, no Compose) → matches Unraid's native Docker UI pattern

Container name, appdata path, and webhook path: **TBD — fill in once the repo exists and
the container is provisioned.**

Public hostname: **TBD** — suggest `anime.***REDACTED-DOMAIN***`, Cloudflare Access-gated to
Andreas only, added manually via the `cloudflare` skill pattern (not something the app or
Claude Code needs standing API access to set up).

## Guardrails — Non-Negotiable

- Never commit secrets, tokens, or API keys. Env vars only, sourced from Vaultwarden
  manually — never hardcoded, never logged.
- Never write to or modify the `unraid-config` repo from this project.
- Never write directly to AniList (no mutations) except through the existing, tested
  sync/recommender jobs — the app itself is read of AniList data, write of personal data
  only.
- Ask before any schema migration that could drop or alter existing columns/data —
  additive migrations (new nullable column, new table) are fine to just do.
- Ask before changing the deploy pipeline itself (webhook, HMAC validation, restart
  script) — this is shared plumbing with other services.
- Never use `docker-compose` or `docker compose` — deploy as a single container via
  `Dockerfile` + `docker run`, matching Unraid's native Docker UI. No `compose.yml` or
  `docker-compose.yml` should exist in this repo.

## Open Decisions (resolve before/during build kickoff)

- [ ] Project/repo name
- [x] Tech stack: **Python + FastAPI + Jinja2 + psycopg2** — server-rendered HTML, no
      frontend build step, one language for both app and sync script. Uvicorn as the ASGI
      server inside a slim Python Docker image.
- [x] Stats view: **embedded Grafana panel** reading Postgres directly — no stats page or
      stats API to build in this app.
- [ ] Container name + appdata path on Unraid
- [ ] Final public hostname
