<!--
Session planning note, kept verbatim as a record of the decision-making behind the
AniDex rename and GPL-3.0 license choice. Written before execution, so it refers to the
project by its pre-rename name in places (`anime-tracker`) and describes steps that have
since been carried out — see git log for the actual commits. Not maintained as living
documentation; treat as historical context only.
-->

# AniDex: image/branding rename + LICENSE + README polish

## Context

`AniDex` (formerly `anime-tracker`) is being prepared for public release. The git-history
credential leak is already fixed and the CI/CD pipeline already migrated to the `AniDex`
repo (verified working end-to-end). Two things remain before this is genuinely
public-ready:

1. The GHCR image, deploy scripts, compose files, and live container names still say
   `anime-tracker` — leftover from before the rename, and confusing for anyone cloning
   `AniDex` and finding an image called something else.
2. No LICENSE file exists yet, and a dependency-license audit was requested before
   picking one.

**License audit result:** checked every dependency, including `crunchyexporter-cli`
(the one baked directly into the Docker image via `git clone`, not just pip-installed —
the highest-risk one). Everything is MIT, BSD-3-Clause, or Apache-2.0, except
`psycopg2-binary` (LGPL-3.0-or-later, which does not impose any obligation on the
*consuming* application — only on modifications to psycopg2 itself). **No dependency
constrains the license choice.** User has chosen **GPL-3.0** — plain GPL-3.0 already
satisfies the "credit me if forked" goal, since redistributing (including forks) requires
preserving the copyright notice. No custom clause needed.

## Scope

**A. Rename image/branding: `anime-tracker` → `anidex`**
**B. Add LICENSE (GPL-3.0) + README/docs polish**

## A. Rename details

New image names: `ghcr.io/napandee/anidex` and `ghcr.io/napandee/anidex-crunchysync`.
Container names also renamed for full consistency: `anime-tracker` → `anidex`,
`anime-tracker-postgres` → `anidex-postgres`, `anime-tracker-runner` → `anidex-runner`.

**Not renaming:** the Unraid appdata directory (`***REDACTED-PATH***/anime-tracker/`) stays
as-is. It's invisible to the public repo/image, and renaming it means moving the live
Postgres data directory — pure risk for zero public-facing benefit. Confirmed via
`docker inspect` that the Postgres container's data lives at a bind mount
(`***REDACTED-PATH***/anime-tracker/postgres` → `/var/lib/postgresql/data`, port
`5433:5432`) that can keep its current path regardless of what the container is named.
Also confirmed the app's `DATABASE_URL` connects via host IP:port
(`***REDACTED-HOST***:5433`), not container-name DNS — so renaming the Postgres container has
zero effect on app connectivity.

**Files to change** (mechanical find/replace of `anime-tracker` → `anidex`, keeping
`-crunchysync` suffix pattern):
- `.github/workflows/build-app.yml` — image tag (build job) and container name +
  image tag (deploy job, currently `docker stop/rm/run anime-tracker` /
  `ghcr.io/$OWNER/anime-tracker:latest`)
- `.github/workflows/build-crunchysync.yml` — image tag
- `scripts/deploy.sh`, `scripts/deploy_crunchysync.sh` — `IMAGE=` and `CONTAINER=` vars
- `compose/anime-tracker.yml` → rename file to `compose/anidex.yml`; update `image:`
  and `container_name:`
- `compose/anime-tracker-postgres.yml` → rename file to `compose/anidex-postgres.yml`;
  update `container_name:` (image itself is upstream `postgres:16-alpine`, unaffected)
- `README.md` — image name examples, and the `git clone` example URL updated to
  `https://github.com/yourname/AniDex.git`

**Live rollout sequence** (after the above is committed and pushed to `AniDex` main):
1. Push triggers `build-app.yml` / `build-crunchysync.yml`, which now build and push
   `ghcr.io/napandee/anidex:latest` and `ghcr.io/napandee/anidex-crunchysync:latest` as
   **brand-new packages**. Unlike the old `anime-tracker` package, these are created by
   `AniDex` itself, so they should get automatic write access with no manual "Manage
   Actions access" grant needed (that was only required before because the *old*
   package had been created by a *different* repo). Will verify this assumption when we
   get there and fix it the same way as before if it turns out not to hold.
2. **Before the deploy job runs**, manually stop+remove the old `anime-tracker`
   container on Unraid via SSH — the deploy job creates a *new* container named
   `anidex` bound to the same port (`8889:8888`), so the old container must free that
   port first or the new one fails to start. One-time manual step.
3. Separately (not part of the GH Actions pipeline — Postgres isn't managed by CI/CD),
   manually rename the Postgres container on Unraid: stop `anime-tracker-postgres`,
   `docker run` a new container named `anidex-postgres` reusing the exact same bind
   mount and port, remove the old container. Data is untouched since it's the same bind
   mount path, just a different container name pointing at it.
4. Separately, rename the runner container: stop+remove `anime-tracker-runner`,
   recreate as `anidex-runner` with the same env file and mounts (`RUNNER_NAME` on the
   GitHub side stays `unraid`, unaffected by the Docker container's own name).
5. Verify: `curl` the app, confirm GHCR shows the new packages, confirm old containers
   are gone and new ones are healthy.

**Out of scope for this plan** (mentioned for awareness, not being done now): the
`homelab-scripts` repo's `github-runners/anime-tracker.yml` and the Unraid
`***REDACTED-PATH***/github-runner/anime-tracker.env` filename are private infra, not part
of the public `AniDex` repo — cosmetic only, can be renamed later if desired, not
required for public-readiness.

## B. LICENSE + docs polish

- Add `LICENSE` at repo root: full GPL-3.0 text, with `Copyright (C) 2026 Andreas
  Brantholm` at the top.
- Add a short **License** section to `README.md` pointing at it.
- Add a disclaimer paragraph to the Crunchyroll sync section of `README.md` noting that
  `crunchyexporter-cli` is an unofficial, community-maintained tool interacting with
  Crunchyroll's session cookies — not officially supported, could break if Crunchyroll
  changes their site, and users should be aware of their own ToS obligations. (Confirmed
  MIT-licensed, so no license concern — this note is about operational/ToS risk, not
  licensing.)

## Verification

- `git log -p` style check isn't needed here (no history rewriting involved this time).
- After rollout: `curl http://***REDACTED-HOST***:8889/` and `/api/stats` for a 200 + real data,
  same pattern used to verify the last two live changes.
- Confirm new GHCR packages exist and are pullable.
- Confirm old `anime-tracker` / `anime-tracker-postgres` / `anime-tracker-runner`
  containers no longer exist and their replacements are `Up` and healthy.
- Spot-check `README.md` renders sensibly end-to-end as a fresh reader would follow it.
