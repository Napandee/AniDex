# Deploy

How a merge reaches production. Host, runner and credential detail is
private — see `.claude/context/deploy.md`.

GitHub Actions CI/CD pipeline:
1. Push to `main` → hosted runner builds the app image and pushes to GHCR
2. Self-hosted runner pulls the new image **by digest** (not `:latest`), stops the old
   container, starts a new one. Deploys are serialized rather than run concurrently —
   pulling by a floating tag let two overlapping deploys race and non-deterministically
   deploy the older of two pushes; pinning by digest plus serializing closed that gap
   (issue #110).

Image tag uses `github.repository_owner` so it works in any fork without config changes.
The deploy job reads env and paths from `vars.APPDATA_PATH` (set as a GitHub repo variable).

See `.github/workflows/build-app.yml` for the full pipeline definition.

Pull requests (including Dependabot's) run `.github/workflows/pr-validate.yml` — a
build-only check (no push, no deploy) that gates merges before the real pipeline ever
touches production. It's grown beyond a plain build since a real 2026-08-13 outage
(issue #50) — a fastapi/starlette Dependabot bump passed CI and a manual smoke test that
only hit a template-free route, then 500'd every real page in production. It now boots
the built image against a real throwaway Postgres service container and smoke-tests both
an authenticated route and a real CSRF-protected POST write (#379), and flags any PR that
adds a new file under `migrations/` with a non-blocking `::warning::` annotation
reminding the author that migrations aren't applied automatically and `LATEST_MIGRATION`
needs bumping in the same PR (#380). `.github/dependabot.yml` proposes weekly version
bumps for `requirements.txt` and both base images; Postgres major-version bumps are
deliberately excluded (see comment
in that file — needs a real migration, not just a new tag).
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

**Applying a migration also means bumping the tracking marker (issue #380).**
`migration_state` (migration 035) is a single-row table holding the highest
migration number confirmed applied to that specific database; `app/main.py`'s
`LATEST_MIGRATION` constant is what the deployed code expects. Admin > Instance
Health compares the two and warns when they're out of sync — the fix for a real
incident (2026-08-27): `028_plex_sync_state.sql` sat unapplied on production for
a while with nobody aware, only caught by accident. Use
`scripts/mark_migration_applied.sh <N>` instead of the raw `docker exec ... psql`
command above whenever possible — it applies the SQL file AND bumps the marker in
one step, so the marker can't be forgotten the way this whole gap was. Whenever a
new migration file is added to the repo, bump `LATEST_MIGRATION` in the same
PR/commit — `pr-validate.yml` reminds you (a non-blocking CI annotation), it
doesn't enforce it.

**002 needs a variable, not a plain stdin pipe.** `002_backfill_and_tighten.sql`
backfills every pre-multi-user row to the instance owner's new `user_id` via a
`:owner_id` psql variable used throughout the file — it must be run with
`-v owner_id=<id>` (e.g. `psql -U ... -d ... -v owner_id=1 -f migrations/002_backfill_and_tighten.sql`),
not piped through `< migrations/002_*.sql` as the generic command above would do, or
`:owner_id` is left unresolved. It also has its own prerequisites (001 already applied;
auth deployed; the owner has logged in once so their real user id is known) — see the
file's own header comment before running it.
