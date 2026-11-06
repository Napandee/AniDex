# Contributing

How AniDex is put together, how to run and test it locally, and how releases are made.

## Stack

| Layer | Tech |
|---|---|
| App | Python + FastAPI + Jinja2, server-rendered HTML, no build step |
| Database | Postgres 16 |
| Container | Docker |
| Scheduler | APScheduler (built into the app container) |
| CI/CD | GitHub Actions → GHCR; releases by hand |

## Architecture

```
Crunchyroll ──► sync_crunchyroll.py ──┐
Netflix ──────► sync_netflix.py ───────┤
Plex ─────────► sync_plex.py ──────────┼──► AniList ──► sync_anilist.py ──► Postgres ──► FastAPI app ──► http://localhost:8888
Prime Video ──► sync_primevideo.py ────┘
```

The app includes a built-in scheduler (APScheduler) that runs the daily AniList sync and
weekly recommender automatically. Schedule is configurable via the Admin page.

Writes back to AniList (bulk status/tag edits, and any Crunchyroll/Netflix/Plex/Prime
Video-originated progress update) go through an async outbox rather than blocking the
request that triggered them — a background worker drains it and retries on transient
AniList failures, so a slow or briefly-down AniList API doesn't stall the UI.

## Local development & pre-merge testing

Verifying a change against the real deployment is risky and slow — this is a second,
throwaway stack for clicking through UI/backend changes on localhost before merging,
built from your local `Dockerfile` instead of pulling from GHCR. It needs no AniList
or Crunchyroll credentials: the app only hard-requires `DATABASE_URL` to boot, and
local email+password auth means the first account you register bootstraps as admin.
The stack sets `ANILIST_MOCK=1`, which skips the live `SaveMediaListEntry` push on the
rating/status/progress endpoints so they can be exercised with no real AniList account —
never set this outside `compose/dev.yml`.

Host ports are auto-assigned rather than fixed, and `scripts/dev/up.sh` /
`scripts/dev/down.sh` always run compose with a project name derived from this
checkout's path — so multiple worktrees/sessions can each bring the stack up at the
same time without colliding on shared container names or ports. (Fixed names/ports
used to mean a second `up --build` from another worktree would silently tear down
and recreate the first one's containers, wiping its throwaway Postgres volume — this
is why raw `docker compose`/`podman compose` invocations against this file are no
longer the documented path.) Use `docker compose` instead of `podman compose` by
setting `COMPOSE_BIN="docker compose"` in the environment before running the scripts.

**1. Bring up the stack** (builds the app image locally, auto-applies `schema.sql` on
first boot via Postgres's init-script mount, prints the assigned app port):

```bash
scripts/dev/up.sh
```

**2. Register the first account** — visit `http://localhost:<printed-port>/auth/register`.
Being the first user, it becomes admin automatically; no invite needed.

**3. Seed some fake library data** so there's something to test against:

```bash
${COMPOSE_BIN:-podman compose} -p "$(cat .dev-stack-project)" -f compose/dev.yml \
  exec -T anidex-dev-postgres \
  psql -U anime_tracker -d anime_tracker \
  -v email="'you@example.com'" \
  -f - < scripts/dev_seed.sql
```

(Use the same email you registered with.) This inserts a handful of `anime` +
`library_entries` rows across a few statuses — enough to exercise drop/complete/status
changes, filters, etc. without touching AniList.

**4. Test in browser** at `http://localhost:<printed-port>`.

**5. Tear down** when done — also drops the throwaway Postgres volume, so nothing
lingers between test runs:

```bash
scripts/dev/down.sh
```

This stack is dev-only — it never touches `compose/anidex.yml` /
`compose/anidex-postgres.yml` or the real deploy pipeline.

**Running the test suite** — unit tests for the sync scripts live under `tests/` and
don't need Postgres or the dev stack running (`tests/conftest.py` stubs the env vars
those scripts read at import time):

```bash
pip install -r requirements-dev.txt
pytest
```

Note CI (`pr-validate.yml`) doesn't run this suite — it builds the image and smoke-tests
one rendered route instead. Run `pytest` locally before opening a PR that touches
`scripts/*.py`.

## CI/CD with GitHub Actions (optional)

Merging to `main` builds the app image and pushes it to GHCR as `ghcr.io/napandee/anidex:<sha>`
(`build-app.yml`). Nothing is deployed by a merge.

To use them in your own fork:

1. Fork the repo
2. Go to **Settings → Actions → Variables** and add:
   - `APPDATA_PATH` — the directory on your host where `.env` lives (e.g. `/opt/anidex`)
3. Add a [self-hosted GitHub Actions runner](https://docs.github.com/en/actions/hosting-your-own-runners)
   on your server with the labels `self-hosted` and `unraid` (or edit the workflow to
   match your own labels)
4. Push to `main` — the build job runs on GitHub's hosted runners and pushes the `<sha>`
   image

The `GHCR_TOKEN` should be a GitHub PAT with `read:packages` scope, stored in your
`APPDATA_PATH/.env` file, for pulling the image onto your host.

### Releases

A deploy happens only when someone runs the **Release** workflow (`release.yml`, started
by hand with a `patch`, `minor` or `major` bump). It:

1. computes the next `vX.Y.Z` from the existing tags;
2. builds and pushes the image once, and gates it with Trivy (fixable HIGH/CRITICAL
   findings fail it);
3. tags that same scanned image `:X.Y.Z`, `:X.Y`, `:X` and `:latest`, with an SBOM and
   provenance;
4. tags the commit and creates the GitHub release with generated notes;
5. builds the fake-data demo, attaches `anidex-hero.jpg`, `anidex-desktop.jpg` and
   `anidex-mobile.jpg` to the release, and publishes the demo to
   [GitHub Pages](https://napandee.github.io/AniDex/).

The maintainer's cluster picks up the released image through Flux image automation.

The release, demo and security-scan workflows are shared: `release.yml`, `demo.yml` and
`security.yml` here only call [Napandee/standard-workflows](https://github.com/Napandee/standard-workflows)
`@v1`, which also holds the demo kit (shim, site check, layout audit, screenshots).
AniDex keeps what is its own in `demo/`: the recorder (`record.mjs`), `build.sh`, the
made-up seed, `demo.config.json` and the audit baseline. Build the demo locally with
`demo/build.sh` (it fetches the kit at `v1` into `demo/.kit`), install the kit's
packages once in the Playwright image:

```sh
podman run --rm -v "$PWD/demo/.kit/demo-kit:/kit:z" -w /kit \
  mcr.microsoft.com/playwright:v1.64.1-noble npm ci --no-audit --no-fund
```

then check it with the kit: `demo/.kit/demo-kit/run.sh check --project .` (also
`audit`, `capture`).

Two more pieces round out the pipeline:

- **PR validation** (`pr-validate.yml`) — builds (but doesn't push or deploy) on every
  pull request, so a broken dependency bump fails its check before you ever merge it. A
  PR that only touches docs/config files skips the actual build and smoke tests (nothing
  there could break them) but still reports the same check, so it can merge normally.
- **Dependabot** (`dependabot.yml`) — proposes weekly version bumps for `requirements.txt`
  and the base images. Postgres major-version bumps are deliberately excluded — that
  needs a real migration, not just a new tag, so it's left as a manual decision.

Optionally, `notify-dependabot.yml` pings a Telegram bot whenever Dependabot opens a PR.
To enable it, add two repo secrets: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` — note
these are separate from the app's own `TELEGRAM_BOT_TOKEN` in `.env` (see
[Notifications](docs/admin/integrations.md#notifications-optional) above — that one's for in-app notifications;
this one's a GitHub Actions secret for repo maintainers).
