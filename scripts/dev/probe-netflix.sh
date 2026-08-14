#!/usr/bin/env bash
# Run this yourself — it needs your real Netflix session cookies, which should never
# be typed into a chat/agent session. Set them in your own shell first (or put them in
# a local .env.netflix.local, gitignored, next to this script's repo root — see
# .env.netflix.local.example), then just run this script with no arguments.
#
#   export NETFLIX_ID_COOKIE='...'
#   export NETFLIX_SECURE_ID_COOKIE='...'
#   scripts/dev/probe-netflix.sh
#
# `podman run -e VARNAME` (no `=value`) passes the value through from your shell's
# environment — it's never written into this script, the podman command line, or
# anything an assistant session would see.
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

podman run --rm \
  -e NETFLIX_ID_COOKIE \
  -e NETFLIX_SECURE_ID_COOKIE \
  anidex-sync-test \
  python3 scripts/dev/probe_netflix_shakti.py
