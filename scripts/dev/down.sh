#!/usr/bin/env bash
# Tears down the stack started by scripts/dev/up.sh for THIS checkout only — the
# project name is derived from this checkout's path, so it never touches another
# worktree/session's stack. -v also drops the throwaway Postgres volume.
#
# Override the compose binary with COMPOSE_BIN=docker\ compose if not using podman.
set -euo pipefail
cd "$(dirname "$0")/../.."

COMPOSE_BIN="${COMPOSE_BIN:-podman compose}"

if [ -f .dev-stack-project ]; then
  PROJECT="$(cat .dev-stack-project)"
else
  REPO_ROOT="$(git rev-parse --show-toplevel)"
  PROJECT="anidex-dev-$(echo -n "$REPO_ROOT" | md5sum | cut -c1-10)"
fi

$COMPOSE_BIN -p "$PROJECT" -f compose/dev.yml down -v
rm -f .dev-stack-project
echo "Stack down (project: ${PROJECT})."
