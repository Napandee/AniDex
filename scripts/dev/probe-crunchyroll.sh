#!/usr/bin/env bash
# Run this yourself — needs your real Crunchyroll etp_rt cookie. Same rule as the
# other scripts/dev/*.sh wrappers: real credentials, your shell only.
#
#   export CRUNCHYROLL_ETP_RT='...'
#   scripts/dev/probe-crunchyroll.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ -f .env.netflix.local ]; then
  set -a
  # shellcheck disable=SC1091
  source .env.netflix.local
  set +a
fi

if [ -z "${CRUNCHYROLL_ETP_RT:-}" ]; then
  echo "ERROR: set CRUNCHYROLL_ETP_RT (env var or .env.netflix.local) first." >&2
  echo "DevTools -> Application/Storage -> Cookies -> https://www.crunchyroll.com -> 'etp_rt'." >&2
  exit 1
fi

podman image exists anidex-sync-test || scripts/dev/build.sh

podman run --rm \
  -e CRUNCHYROLL_ETP_RT \
  anidex-sync-test \
  python3 scripts/dev/probe_crunchyroll_history.py
