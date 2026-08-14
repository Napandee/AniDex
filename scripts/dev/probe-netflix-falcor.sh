#!/usr/bin/env bash
# Run this yourself — needs your real Netflix cookies (see setup-netflix-env.sh).
# Same rule as the other scripts/dev/*.sh wrappers: real credentials, your shell only.
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ -f .env.netflix.local ]; then
  set -a
  # shellcheck disable=SC1091
  source .env.netflix.local
  set +a
fi

if [ -z "${NETFLIX_COOKIE_HEADER:-}" ] || [ -z "${NETFLIX_PROFILE_GUID:-}" ]; then
  echo "ERROR: set NETFLIX_COOKIE_HEADER and NETFLIX_PROFILE_GUID (env vars or .env.netflix.local) first." >&2
  echo "Run scripts/dev/setup-netflix-env.sh to (re-)populate .env.netflix.local." >&2
  exit 1
fi

podman image exists anidex-sync-test || scripts/dev/build.sh

podman run --rm \
  -e NETFLIX_COOKIE_HEADER \
  -e NETFLIX_PROFILE_GUID \
  anidex-sync-test \
  python3 scripts/dev/probe_netflix_falcor.py
