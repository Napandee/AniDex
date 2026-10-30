#!/bin/sh
[ -z "${HEAVY_SLOT:-}" ] && [ -x ~/.claude/skills/test-chain/heavy.sh ] && exec env HEAVY_CLASS=large ~/.claude/skills/test-chain/heavy.sh sh "$0" "$@"
# Builds the static demo into _site/ (record, then replay):
#   1. covers   demo/make_covers.py -> demo/covers/*.svg
#   2. deps     npm ci in demo/ (Playwright image, browsers already inside)
#   3. image    the app image from the repo Dockerfile
#   4. record   the sealed stack in demo/compose.yml: db + app + recorder on an
#               internal network with no egress; the recorder writes _site/
# Steps 1-3 need the network; step 4 has none. Everything runs in containers.
#
# Local runs gate themselves through the test chain's heavy.sh (one large slot);
# where heavy.sh is absent (CI) this runs directly. Uses `<engine> compose` when the
# engine has a compose provider, else the same stack with plain podman/docker
# commands. Exits non-zero on any recorder error. Logs: demo/.build/.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

PW_IMAGE=mcr.microsoft.com/playwright:v1.64.1-noble
PY_IMAGE=docker.io/library/python:3.12-slim
PG_IMAGE=docker.io/library/postgres:16-alpine
APP_IMAGE=${ANIDEX_DEMO_APP_IMAGE:-anidex-demo-app:local}
LOGS=$ROOT/demo/.build
mkdir -p "$LOGS"

if [ -n "${ENGINE:-}" ]; then :
elif command -v podman >/dev/null 2>&1; then ENGINE=podman
else ENGINE=docker
fi

say() { printf '== %s\n' "$*"; }
# Runs the recorder, showing and logging its output; status = its exit code.
record() {
  { rc=0; "$@" 2>&1 || rc=$?; echo "$rc" >"$LOGS/record.rc"; } | tee "$LOGS/record.log"
  status=$(cat "$LOGS/record.rc")
}

say "covers"
$ENGINE run --rm -v "$ROOT:/w:z" -w /w "$PY_IMAGE" python3 demo/make_covers.py

say "recorder dependencies"
$ENGINE run --rm -v "$ROOT/demo:/w:z" -w /w -e PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
  "$PW_IMAGE" npm ci --no-audit --no-fund --loglevel=error

say "app image"
$ENGINE build -q -t "$APP_IMAGE" -f Dockerfile . >/dev/null

say "record (sealed network)"
status=0
if $ENGINE compose version >/dev/null 2>&1; then
  export ANIDEX_DEMO_APP_IMAGE="$APP_IMAGE"
  compose() { $ENGINE compose -f demo/compose.yml -p anidex-demo "$@"; }
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  record compose up --no-build --abort-on-container-exit --exit-code-from recorder recorder
  compose logs anidex >"$LOGS/app.log" 2>&1 || true
  compose down -v --remove-orphans >/dev/null 2>&1 || true
else
  # Same stack as demo/compose.yml, for engines without a compose provider.
  P=anidex-demo-$$
  NET=$P-net
  DB_ENV="-e POSTGRES_DB=anidex_demo -e POSTGRES_USER=anidex_demo -e POSTGRES_PASSWORD=demo-only-not-a-secret"
  DB_URL=postgresql://anidex_demo:demo-only-not-a-secret@db:5432/anidex_demo
  cleanup() {
    $ENGINE logs "$P-app" >"$LOGS/app.log" 2>&1 || true
    $ENGINE rm -f "$P-recorder" "$P-app" "$P-db" >/dev/null 2>&1 || true
    $ENGINE network rm "$NET" >/dev/null 2>&1 || true
  }
  trap cleanup EXIT INT TERM
  $ENGINE network create --internal "$NET" >/dev/null
  # shellcheck disable=SC2086
  $ENGINE run -d --name "$P-db" --network "$NET" --network-alias db $DB_ENV \
    -v "$ROOT/schema.sql:/docker-entrypoint-initdb.d/01-schema.sql:ro,z" "$PG_IMAGE" >/dev/null
  i=0
  until $ENGINE exec "$P-db" pg_isready -q -U anidex_demo -d anidex_demo 2>/dev/null; do
    i=$((i + 1)); [ "$i" -gt 60 ] && { echo "ERROR db not ready"; exit 1; }; sleep 1
  done
  # initdb restarts the server once after loading schema.sql; wait for the final one.
  sleep 2
  until $ENGINE exec "$P-db" pg_isready -q -U anidex_demo -d anidex_demo 2>/dev/null; do sleep 1; done
  $ENGINE run -d --name "$P-app" --network "$NET" --network-alias anidex \
    -e DATABASE_URL="$DB_URL" \
    -e SESSION_SECRET_KEY=demo-only-not-for-real-use \
    -e SETTINGS_ENCRYPTION_KEY=8CgnHDlfBgM3RQ9L0M_tjLWCtET3Cv8YdauDtn0bkfc= \
    -e TZ=UTC -e ANILIST_MOCK=1 -e ANIDEX_DISABLE_SCHEDULER=1 -e HEAVY_CORES="${HEAVY_CORES:-}" \
    -v "$ROOT/demo/covers:/app/app/static/demo-covers:ro,z" \
    "$APP_IMAGE" sh -c 'exec ${HEAVY_CORES:+taskset -c "$HEAVY_CORES"} uvicorn app.main:app --host 0.0.0.0 --port 8888' >/dev/null
  record $ENGINE run --rm --name "$P-recorder" --network "$NET" -w /work \
    -e APP_URL=http://anidex:8888 -e DATABASE_URL="$DB_URL" -e OUT=/work/_site -e HEAVY_CORES="${HEAVY_CORES:-}" \
    -v "$ROOT:/work:z" "$PW_IMAGE" \
    sh -c 'exec ${HEAVY_CORES:+taskset -c "$HEAVY_CORES"} node demo/record.mjs'
fi

if [ "$status" -ne 0 ] || grep -q '^ERROR' "$LOGS/record.log"; then
  echo "demo build FAILED (see $LOGS/record.log, $LOGS/app.log)" >&2
  exit 1
fi
[ -s "$ROOT/_site/index.html" ] && [ -s "$ROOT/_site/fixtures.json" ] || {
  echo "demo build FAILED: _site/index.html or _site/fixtures.json missing" >&2
  exit 1
}
say "done: $(grep '^RECORDED' "$LOGS/record.log")"
