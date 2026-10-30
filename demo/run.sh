#!/bin/sh
[ -z "${HEAVY_SLOT:-}" ] && [ -x ~/.claude/skills/test-chain/heavy.sh ] && exec env HEAVY_CLASS=large ~/.claude/skills/test-chain/heavy.sh sh "$0" "$@"
# Runs a demo browser job in the Playwright image, with no network:
#   demo/run.sh check [siteDir]                     site check (demo/check.mjs)
#   demo/run.sh audit [siteDir] [--update-baseline] layout audit (demo/audit.mjs)
#   demo/run.sh test                                node --test 'tests/demo/*.test.mjs'
# siteDir defaults to _site (built by demo/build.sh, which also installs
# demo/node_modules). Local runs gate themselves through the test chain's
# heavy.sh (one large slot); where heavy.sh is absent (CI) this runs directly.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
PW_IMAGE=mcr.microsoft.com/playwright:v1.64.1-noble

if [ -n "${ENGINE:-}" ]; then :
elif command -v podman >/dev/null 2>&1; then ENGINE=podman
else ENGINE=docker
fi

[ -d demo/node_modules/playwright ] || {
  echo "demo/node_modules missing: run demo/build.sh first" >&2
  exit 1
}

job=${1:-}
[ $# -gt 0 ] && shift
case "$job" in
  check|audit)
    case "${1:-}" in ""|--*) set -- _site "$@" ;; esac
    set -- node "demo/$job.mjs" "$@" ;;
  test) set -- node --test 'tests/demo/*.test.mjs' ;;
  *) echo "usage: demo/run.sh check|audit|test [siteDir] [--update-baseline]" >&2; exit 2 ;;
esac

exec $ENGINE run --rm --network none -v "$ROOT:/work:z" -w /work \
  -e HEAVY_CORES="${HEAVY_CORES:-}" "$PW_IMAGE" \
  sh -c 'exec ${HEAVY_CORES:+taskset -c "$HEAVY_CORES"} "$@"' sh "$@"
