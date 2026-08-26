#!/usr/bin/env bash
# Run this yourself — needs your real Prime Video cookies (see setup-primevideo-env.sh).
# Same rule as the other scripts/dev/*.sh wrappers: real credentials, your shell only.
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ -f .env.primevideo.local ]; then
  set -a
  # shellcheck disable=SC1091
  source .env.primevideo.local
  set +a
fi

if [ -z "${PRIMEVIDEO_COOKIE_HEADER:-}" ]; then
  echo "ERROR: set PRIMEVIDEO_COOKIE_HEADER (env var or .env.primevideo.local) first." >&2
  echo "Run scripts/dev/setup-primevideo-env.sh to (re-)populate .env.primevideo.local." >&2
  exit 1
fi

# Always rebuild rather than only-if-missing: the image is a COPY snapshot of
# scripts/ at build time, not a live mount, so a stale image silently runs old
# code with no error — bit us once already. Layer caching keeps this cheap
# unless requirements-dev.txt changed.
scripts/dev/build.sh

podman run --rm \
  -e PRIMEVIDEO_COOKIE_HEADER \
  -e PRIMEVIDEO_NEXT_TOKEN \
  -e PRIMEVIDEO_PROBE_MAX_PAGES \
  anidex-sync-test \
  python3 scripts/dev/probe_primevideo_history.py
