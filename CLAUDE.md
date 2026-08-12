# AniDex — Project Context for Claude Code

## Purpose

A personal anime tracking, rating, and recommendation site. AniList is the system of
record for catalog data and list status; this app adds the personal layer AniList's own
UI doesn't support well (drop reasons, custom tags, a real "watch next" queue) and pulls
it all into one self-hosted page.

## Scope

**In scope:**
- Read-only display of AniList library (status, score-as-stars, progress, cover art)
- Where-to-watch links pulled from AniList's `externalLinks` / `streamingEpisodes`
- Personal notes layer: drop reasons, custom tags, freeform notes, manual queue priority
- "Watch next" queue driven by the recommender job's output
- Upcoming-episode view for anything in Watching/Planning status
- Built-in stats page (watch time, completion rate, score distribution, top genres)

**Out of scope — do not build these:**
- No user auth / login system — access control is the operator's responsibility (reverse proxy, Cloudflare Access, etc.)
- No re-scraping Crunchyroll directly — AniList is the only data source this app talks to
- No rebuilding AniList's catalog search/browse UI — link out to AniList for that
- No payment, sharing, or multi-user features — this is single-user

## Data Source

AniList GraphQL API — `https://graphql.anilist.co`, POST requests, no auth needed for
public reads, OAuth token needed for anything under the user's own list. Rate limit:
90 req/min on the free tier — batch queries, don't loop one-anime-per-request where a
paginated query works.

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

- **Sync job**: `scripts/run_full_sync.py`, run inside the app container (via the
  built-in scheduler or the manual "Sync Now" trigger). Chains three steps: Crunchyroll
  history fetch via crunchyexporter-cli (skipped if no CR credentials configured) →
  CR→AniList progress sync (`sync_crunchyroll.py`) → AniList→Postgres sync
  (`sync_anilist.py`). Upserts into `anime` / `library_entries` / `airing_schedule_cache`
  / `cr_sync_state`. There is no separate sync container — `crunchyexporter-cli` is
  vendored directly into the main Dockerfile.
- **Recommender job**: runs `run_recommender.py`. Scores unwatched/planning anime against
  taste profile, writes to `recommendation_scores`. Never touches the `dismissed` flag.
- **App**: reads all tables; writes to `personal_notes`, the `dismissed` flag on
  `recommendation_scores`, and `library_entries.score` (via the rating endpoint).
  Also pushes ratings, status, and progress to AniList via `SaveMediaListEntry` in real-time.
- **Built-in scheduler**: APScheduler runs inside the app container. Daily sync and weekly
  recommender fire automatically; schedule is configurable via the Settings page.

## Deploy

GitHub Actions CI/CD pipeline:
1. Push to `main` → hosted runner builds the app image and pushes to GHCR
2. Self-hosted runner pulls the new image, stops the old container, starts a new one

Image tag uses `github.repository_owner` so it works in any fork without config changes.
The deploy job reads env and paths from `vars.APPDATA_PATH` (set as a GitHub repo variable).

See `.github/workflows/build-app.yml` for the full pipeline definition.

Pull requests (including Dependabot's) run `.github/workflows/pr-validate.yml` — a
build-only check (no push, no deploy) that gates merges before the real pipeline ever
touches production. `.github/dependabot.yml` proposes weekly version bumps for
`requirements.txt` and both base images; Postgres major-version bumps are deliberately
excluded (see comment in that file — needs a real migration, not just a new tag).
`.github/workflows/notify-dependabot.yml` pings Telegram when a Dependabot PR opens.

**GHCR gotcha worth knowing:** a package's automatic `GITHUB_TOKEN` write access is tied
to whichever repo *first created* it, not the current repo — if a package ever needs to
move to a differently-named/forked repo, the new repo needs an explicit grant under
`Manage Actions access` on the package's settings page, or every build fails with
`permission_denied: write_package` no matter what `permissions:` the workflow declares.

## Guardrails — Non-Negotiable

- Never commit secrets, tokens, or API keys. Env vars only — never hardcoded, never logged.
- The app writes to AniList only via three endpoints: rating (`POST /api/anime/{id}/rating`),
  status (`POST /api/anime/{id}/status`), and progress (`POST /api/anime/{id}/progress`),
  all using `SaveMediaListEntry`. Never add further AniList mutations to the app without
  explicit agreement.
- Ask before any schema migration that could drop or alter existing columns/data —
  additive migrations (new nullable column, new table) are fine to just do.
- Ask before changing the deploy pipeline (GitHub Actions workflows, image name) — changes
  here affect the live deployment path.
- The deploy pipeline uses `docker pull` + `docker run` (no Compose) driven by the GitHub
  Actions workflow. Compose files in `compose/` exist solely for container managers that
  track containers via Compose; they are not the deployment mechanism.

## Decisions Made

- **Tech stack**: Python + FastAPI + Jinja2 + psycopg2 — server-rendered HTML, no
  frontend build step. Uvicorn inside a slim Python Docker image.
- **Stats**: built-in stats page served from `/stats` — no external dashboard dependency.
- **Single-user**: no auth layer in the app itself. Deploy behind a reverse proxy or
  access control tool of your choice.
- **License**: GPL-3.0. Dependency audit (Aug 2026) confirmed no dependency — including
  `crunchyexporter-cli`, vendored via git rather than pip — imposes a stricter license
  that would have constrained this choice.
- **One sync path, not two**: `crunchyexporter-cli` is vendored once, in the main
  Dockerfile only. There used to be a second, standalone `crunchysync` image/container;
  it was removed (Aug 2026) because it was strictly redundant with the in-app scheduler
  and manual "Sync Now" trigger, and its existence let the tool's pin drift out of sync
  between two Dockerfiles. Don't reintroduce a second container for this without a real
  new reason — isolation/scheduling needs the first one never actually served.
