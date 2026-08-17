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
- Multi-user: local email+password auth by default, Google/Discord OAuth optional
  per-instance, invite-only signup, admin-managed. See Decisions Made.
- Cross-user "also watching" indicator (opt-in per-user hidden tags/genres and
  anonymized-activity controls; nothing surfaced by default) — see `app/privacy.py`.
  Static/on-demand only; shipped as issue #29. Collaborative-filtering
  recommendations (weighting candidates by other users' ratings) shipped as
  issue #31 — see `run_recommender.py`'s `fetch_cross_user_signal`/
  `CROSS_USER_WEIGHT`. Issue #16 (rolling activity-feed banner, cross-user
  episode comments) builds further on this and is still open, gated on an
  explicit scope decision — not in scope yet.
- In-library search, plus a quick-add-by-title lookup against AniList (not a full catalog
  browse UI — see Out of scope below)

**Out of scope — do not build these:**
- No re-scraping Crunchyroll directly — AniList is the only data source this app talks to
- No rebuilding AniList's catalog search/browse UI — link out to AniList for that
- No payment or public sharing features — invite-only multi-user is the ceiling here,
  not a social/sharing platform

## Data Source

AniList GraphQL API — `https://graphql.anilist.co`, POST requests, no auth needed for
public reads, OAuth token needed for anything under the user's own list. Rate limit:
90 req/min on the free tier — batch queries, don't loop one-anime-per-request where a
paginated query works.

Streaming links (`externalLinks`, `streamingEpisodes`) are community-curated on AniList's
side and can lag real availability — show a "last synced" timestamp next to them rather
than presenting them as guaranteed-current.

## Data Model

See `schema.sql` in repo root. Three categories, kept in separate tables on purpose:
- **AniList-sourced** (`anime`, `library_entries`, `airing_schedule_cache`) — fully
  rebuildable by the sync job. Never hand-edit rows in these tables.
- **Personal layer** (`personal_notes`, `recommendation_scores`) — the actual reason this
  app exists. Sync jobs must never write to `personal_notes`; `recommendation_scores` is
  rebuilt by the recommender job but must preserve the `dismissed` flag across rebuilds.
- **Auth/instance** (`users`, `invites`, `instance_config`, `password_resets`,
  `notified_episodes`) — added for multi-user (Aug 2026). Neither AniList-sourced nor
  personal-layer; sync jobs never touch these either. `library_entries` /
  `personal_notes` / `recommendation_scores` / `cr_sync_state` / `sync_log` /
  `settings` / `notified_episodes` all carry a `user_id` scoping every row to one
  account.

## Architecture

- **Sync job**: `scripts/run_full_sync.py`, single-user primitive — always invoked with a
  `USER_ID` env var, either by the manual "Sync Now" trigger (that user only) or the
  built-in scheduler's loop over every user with credentials configured (sequential, one
  user's failure caught and logged without blocking the rest — see `_scheduled_full_sync`
  in `app/main.py`). Chains three steps: CR→AniList progress sync (`sync_crunchyroll.py`,
  skipped if no CR credentials configured for that user) → Netflix→AniList progress sync
  (`sync_netflix.py`, skipped if no Netflix credentials configured) → AniList→Postgres
  sync (`sync_anilist.py`). Both `sync_crunchyroll.py` and `sync_netflix.py` fetch their
  respective service's watch history directly (cookie-authenticated API clients, no
  vendored third-party CLI, no intermediate history file) — newest-first, stopping at a
  Postgres-backed per-user watermark (`cr_sync_state.last_seen_watched_at` /
  `netflix_sync_state.last_seen_watched_at`) so a routine sync only walks genuinely new
  activity. Upserts into `anime` / `library_entries` / `airing_schedule_cache` /
  `cr_sync_state` / `netflix_sync_state`, all scoped to that user except
  `anime`/`airing_schedule_cache` which stay global. There is no separate sync container.
- **Recommender job**: runs `run_recommender.py`, same per-user/`USER_ID` pattern as the
  sync job. Scores unwatched/planning anime against that user's taste profile, writes to
  `recommendation_scores`. Never touches the `dismissed` flag.
- **App**: reads all tables, scoped to the logged-in user; writes to `personal_notes`, the
  `dismissed` flag on `recommendation_scores`, and `library_entries.score` (via the rating
  endpoint). Also pushes ratings, status, and progress to AniList via `SaveMediaListEntry`
  in real-time.
- **Built-in scheduler**: APScheduler runs inside the app container. Daily sync and weekly
  recommender fire automatically for every eligible user; schedule *time* is instance-wide
  (one cron trigger regardless of user count), configurable via Settings, admin-only.

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

**Schema migrations are manual, not part of the deploy pipeline.** `migrations/` holds
numbered SQL files (`001_add_multi_user.sql`, `002_backfill_and_tighten.sql`) for
upgrading an already-running instance's database — nothing in the Dockerfile or GitHub
Actions applies them automatically; they're run by hand against the live Postgres
(`docker exec -i <postgres-container> psql -U ... -d ... < migrations/00N_*.sql`) as a
deliberate, separate step from the code deploy. `schema.sql` is the fresh-install target
schema; migrations exist only for the upgrade path. Per the guardrail below, always back
up first and get explicit confirmation before running one against real data.

**002 needs a variable, not a plain stdin pipe.** `002_backfill_and_tighten.sql`
backfills every pre-multi-user row to the instance owner's new `user_id` via a
`:owner_id` psql variable used throughout the file — it must be run with
`-v owner_id=<id>` (e.g. `psql -U ... -d ... -v owner_id=1 -f migrations/002_backfill_and_tighten.sql`),
not piped through `< migrations/002_*.sql` as the generic command above would do, or
`:owner_id` is left unresolved. It also has its own prerequisites (001 already applied;
auth deployed; the owner has logged in once so their real user id is known) — see the
file's own header comment before running it.

## Guardrails — Non-Negotiable

- Track bugs, enhancements, and research spikes as GitHub issues (use
  `.github/ISSUE_TEMPLATE/task.md`) before starting work on them, not just in commit
  messages or chat — the reasoning behind scope/tradeoffs needs to be findable later
  without digging through history. When work actually starts: assign the issue
  (`gh issue edit <n> --add-assignee Napandee`) and reference it in the eventual
  commit(s) with a closing keyword (`Fixes #n` / `Closes #n`) so it auto-closes on
  merge — that's the real link between an issue and the code that resolved it, not
  a manual comment.
- Merge multi-commit feature branches with a real merge commit (`gh pr merge --merge`),
  not squash — pass the flag explicitly rather than relying on whatever the repo's
  default merge method happens to be. Each commit stays individually walkable/revertable
  in `main`'s real history instead of folded into one. (`feature/multi-user` was
  squash-merged before this was decided — nothing was actually lost, since GitHub's
  squash concatenates every commit message into the squash commit's body and the PR
  page keeps the original commits browsable regardless — but don't rely on that as the
  plan going forward.)
- Never commit secrets, tokens, or API keys. Env vars only — never hardcoded, never logged.
- If a secret, internal IP, or internal filesystem path is ever committed anyway:
  rotating/invalidating the credential is necessary but not sufficient on its own.
  Forward-fixing (removing it from the latest commit) leaves the original value fully
  intact and retrievable in every commit before that fix — treat the repo as still
  compromised until the plaintext is actually gone from history
  (`git filter-repo` + force-push), not just from the current tip. This happened for
  real (2026-08-12 Postgres password, fixed forward only; still sitting in history
  until a full pre-public-release audit caught it on 2026-08-16).
- Before this repo is ever made public: a force-push history rewrite is necessary but
  **not sufficient** if the repo has any PR history. GitHub retains every merged/closed
  PR's original commits via server-side `refs/pull/N/head` refs — and serves them
  directly on the PR's own web page — regardless of what happens to the branches
  afterward; there's no client-side way to remove these. Either get GitHub Support to
  purge cached PR data first, or recreate the repo from scratch (new empty repo, push
  only clean history, migrate open issues, retire the old repo as a permanently-private
  archive). Confirmed the hard way on 2026-08-16 — a branch-only rewrite left the
  original secrets fully browsable through old PRs even after the "clean" push.
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
- **Multi-user** (pivoted from the original single-user design, Aug 2026): the app now
  has its own auth layer rather than relying solely on the reverse proxy / access
  control tool in front of it. Local email+password is the zero-config default; Google
  and Discord OAuth are optional, admin-configured per-instance via
  `/admin/oauth-settings`. Signup is invite-only except for the very first account on
  an empty `users` table, which bootstraps as admin automatically — invites for
  subsequent accounts are issued via the separate `/admin/invites` endpoint. Every
  route and sync script (`run_full_sync.py`,
  `sync_anilist.py`, `sync_crunchyroll.py`, `run_recommender.py`) is scoped to the
  logged-in/invoking user; the sync schedule *time* stays instance-wide (one cron
  trigger regardless of user count), gated to admins in Settings. Account linking
  (attaching Google/Discord to an existing account) is explicit-only — never automatic
  by email match, since that would mean trusting a bare provider-supplied email as
  proof of identity. Linking only ever happens via `/settings/link/{provider}` while
  already authenticated, through a callback route (`/auth/link-callback/{provider}`)
  kept deliberately separate from the ordinary login callback so it can't fire as a
  side effect of an ordinary login click. Reverse-proxy access control (Cloudflare
  Access, etc.) is still expected as an outer layer, especially while invite-only
  signup keeps this to a small trusted group — the app's own auth doesn't replace it,
  it adds a second, inner gate.
- **License**: GPL-3.0. Dependency audit (Aug 2026) confirmed no dependency — including
  `crunchyexporter-cli`, vendored via git rather than pip — imposes a stricter license
  that would have constrained this choice.
- **One sync path, not two**: there used to be a second, standalone `crunchysync`
  image/container; it was removed (Aug 2026) because it was strictly redundant with
  the in-app scheduler and manual "Sync Now" trigger. Don't reintroduce a second
  container for this without a real new reason — isolation/scheduling needs the
  first one never actually served. `crunchyexporter-cli` itself (vendored via git
  into the main Dockerfile, the thing the "second container" duplicated the pin
  of) was later retired entirely (issue #35) once `sync_crunchyroll.py` grew its
  own direct-API fetch matching `sync_netflix.py`'s pattern — nothing in this repo
  vendors a third-party CR/Netflix client anymore.
