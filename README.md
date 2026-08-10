# anime-tracker

A personal anime tracking, rating, and recommendation site — built on top of AniList's
catalog data, with a personal layer AniList's own UI doesn't give a good home to.

Live at `anime.***REDACTED-DOMAIN***` — Cloudflare Access-gated to Andreas only.

## Why this exists

AniList's UI doesn't track *why* something got dropped after two episodes, or give a real
"watch next" queue driven by your own taste. This project fills that gap:

- **AniList as system of record**: catalog data, list status, scores, and streaming links
  pulled from AniList's GraphQL API — never re-scraped from Crunchyroll directly
- **Crunchyroll sync**: watch history and progress synced from Crunchyroll into AniList
  via [crunchyexporter-cli](https://github.com/ruflas/crunchyexporter-cli)
- **Personal layer**: drop reasons, custom tags, freeform notes, and manual queue-priority
  — none of which AniList has a structured place for
- **Recommender**: scores unwatched/planning anime against the genres, tags, and studios
  of your highest-rated completed shows
- **Upcoming-episode tracking**: airing schedule for anything in Watching/Planning status

## Stack

| Layer | Tech |
|---|---|
| App | Python + FastAPI + Jinja2, server-rendered HTML |
| DB | Postgres (persisted in Unraid appdata) |
| Container | Docker, image on GHCR (`ghcr.io/napandee/anime-tracker`) |
| CI/CD | GitHub Actions → GHCR → self-hosted runner on Unraid |
| Hosting | Unraid (`anime-tracker` container), port 8889 |
| Access | Cloudflare Tunnel + Access |

## Repo contents

| Path | Purpose |
|---|---|
| `CLAUDE.md` | Full scope, architecture, guardrails, and decisions for agentic development |
| `schema.sql` | Postgres schema — AniList-sourced tables kept separate from personal-layer tables |
| `app/` | FastAPI app — routes, templates, static assets |
| `scripts/` | `sync_anilist.py`, `sync_crunchyroll.py`, `run_recommender.py`, `deploy.sh` |
| `Dockerfile` | App image |
| `Dockerfile.crunchysync` | Sync + recommender image (`ghcr.io/napandee/anime-tracker-crunchysync`) |
| `compose/` | Compose files for Unraid's Compose Manager Plus (tracking only, not used for deploy) |
| `.github/workflows/` | `build-app.yml` and `build-crunchysync.yml` — CI/CD pipelines |

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
                                                          anime.***REDACTED-DOMAIN***
```

**Sync job** — n8n "Anime Tracker — Daily Sync" (04:30 daily): Crunchyroll history →
AniList progress sync → AniList→Postgres upsert.

**Recommender job** — n8n "Anime Tracker — Weekly Recommender" (Sundays 05:00): scores
unwatched/planning anime against taste profile, writes to `recommendation_scores`.

**Deploy pipeline** — push to `main` → GitHub Actions builds and pushes image to GHCR →
self-hosted runner on Unraid pulls image and restarts container. Runner config lives in
`homelab-scripts/github-runners/anime-tracker.yml`.

## Environment variables

Stored in `***REDACTED-PATH***/anime-tracker/.env` on Unraid (never committed).

| Variable | Purpose |
|---|---|
| `ANILIST_TOKEN` | OAuth token for AniList mutations (rating, status, progress) |
| `POSTGRES_HOST` | Postgres host |
| `POSTGRES_DB` | Database name |
| `POSTGRES_USER` | DB user |
| `POSTGRES_PASSWORD` | DB password |
| `GRAFANA_PUBLIC_URL` | Public Grafana URL for the stats page embed |
| `GRAFANA_EMBED_URL` | Internal Grafana URL used server-side for embed src (optional, falls back to public) |
| `GHCR_TOKEN` | GitHub PAT with `read:packages` + `repo` scope — used by deploy job to pull from GHCR |
| `TZ` | Container timezone (e.g. `Europe/London`) |

See `CLAUDE.md` for full architecture detail, data model, and guardrails before making changes.
