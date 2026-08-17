#!/usr/bin/env bash
# Brings up compose/dev.yml with a project name derived from this checkout's path,
# so concurrent worktrees/sessions each get their own isolated stack instead of
# colliding on shared container names (podman-compose/docker-compose otherwise
# derive the project name from the compose/ directory basename, which is the same
# for every checkout). Host ports are auto-assigned in compose/dev.yml for the same
# reason — this script prints the one that got assigned to the app.
#
# Override the compose binary with COMPOSE_BIN=docker\ compose if not using podman.
set -euo pipefail
cd "$(dirname "$0")/../.."

COMPOSE_BIN="${COMPOSE_BIN:-podman compose}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
PROJECT="anidex-dev-$(echo -n "$REPO_ROOT" | md5sum | cut -c1-10)"
echo "$PROJECT" >.dev-stack-project

$COMPOSE_BIN -p "$PROJECT" -f compose/dev.yml up --build -d

PORT=""
for _ in $(seq 1 30); do
  PORT=$($COMPOSE_BIN -p "$PROJECT" -f compose/dev.yml port anidex-dev 8888 2>/dev/null | cut -d: -f2 || true)
  [ -n "$PORT" ] && break
  sleep 1
done

if [ -z "$PORT" ]; then
  echo "Could not determine the assigned app port — check '$COMPOSE_BIN -p $PROJECT -f compose/dev.yml ps'" >&2
  exit 1
fi

echo "Stack up — app: http://localhost:${PORT}  (project: ${PROJECT})"
