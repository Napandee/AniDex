#!/usr/bin/env bash
# Run this yourself — same rule as probe-netflix.sh: real credentials, your shell only.
# Runs sync_netflix.py in DRY_RUN mode (see scripts/sync_netflix.py's own docstring) —
# reads your real AniList library, logs every AniList update it *would* make, never
# calls anilist_update() or touches any database.
#
#   export NETFLIX_ID_COOKIE='...'
#   export NETFLIX_SECURE_ID_COOKIE='...'
#   export ANILIST_TOKEN='...'          # optional — omit to use the image's dummy
#   export ANILIST_USERNAME='...'       # value, but then no real matches will be found
#   scripts/dev/dryrun-netflix.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ -f .env.netflix.local ]; then
  set -a
  # shellcheck disable=SC1091
  source .env.netflix.local
  set +a
fi

if [ -z "${NETFLIX_ID_COOKIE:-}" ] || [ -z "${NETFLIX_SECURE_ID_COOKIE:-}" ]; then
  echo "ERROR: set NETFLIX_ID_COOKIE and NETFLIX_SECURE_ID_COOKIE (env vars or .env.netflix.local) first." >&2
  exit 1
fi

podman image exists anidex-sync-test || scripts/dev/build.sh

# Only pass -e for ANILIST_TOKEN/ANILIST_USERNAME if actually set in the caller's
# shell — otherwise podman would pass through an empty value and override the
# image's dummy default with a blank, which fetch_user_list() would reject.
ANILIST_ENV_ARGS=()
[ -n "${ANILIST_TOKEN:-}" ] && ANILIST_ENV_ARGS+=(-e ANILIST_TOKEN)
[ -n "${ANILIST_USERNAME:-}" ] && ANILIST_ENV_ARGS+=(-e ANILIST_USERNAME)

podman run --rm \
  -e NETFLIX_ID_COOKIE \
  -e NETFLIX_SECURE_ID_COOKIE \
  -e DRY_RUN=1 \
  "${ANILIST_ENV_ARGS[@]}" \
  anidex-sync-test \
  python3 scripts/sync_netflix.py
