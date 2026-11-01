#!/bin/sh
[ -z "${HEAVY_SLOT:-}" ] && [ -x ~/.claude/skills/test-chain/heavy.sh ] && exec env HEAVY_CLASS=large ~/.claude/skills/test-chain/heavy.sh sh "$0" "$@"
# Builds the static demo into _site/ (record, then replay):
#   1. covers   demo/make_covers.py -> demo/covers/*.svg
#   2. deps     npm ci in demo/ (Playwright image, browsers already inside)
#   3. image    the app image from the repo Dockerfile, plus app and Postgres
#               images with libfaketime, so the stack's clock starts at the
#               pinned "now" in demo/demo.config.json (same data every rebuild)
#   4. record   the sealed stack: db + app + recorder containers on an internal
#               network with no egress and no published port; the recorder
#               writes _site/
# Steps 1-3 need the network; step 4 has none. Everything runs in containers.
#
# Local runs gate themselves through the test chain's heavy.sh (one large slot);
# where heavy.sh is absent (CI) this runs directly. The stack is plain podman or
# docker commands (ENGINE picks one; default podman if present): one definition of
# the sealed stack, the same locally and on CI. Exits non-zero on any recorder
# error. Logs: demo/.build/.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
# The shared demo kit (Napandee/standard-workflows demo-kit): the shim the
# recorder injects comes from it. CI's shared workflows set DEMO_KIT to their
# checkout; a local run fetches the kit at v1 into demo/.kit (gitignored).
if [ -z "${DEMO_KIT:-}" ]; then
  [ -d "$ROOT/demo/.kit/.git" ] || git clone -q --depth 1 --branch v1 https://github.com/Napandee/standard-workflows "$ROOT/demo/.kit"
  DEMO_KIT=$ROOT/demo/.kit/demo-kit
fi
[ -f "$DEMO_KIT/shim/demo-shim.js" ] || { echo "demo kit not found at $DEMO_KIT" >&2; exit 1; }
cd "$ROOT"

PW_IMAGE=mcr.microsoft.com/playwright:v1.64.1-noble
PY_IMAGE=docker.io/library/python:3.12-slim
PG_IMAGE=docker.io/library/postgres:16-alpine
APP_IMAGE=${ANIDEX_DEMO_APP_IMAGE:-anidex-demo-app:local}
APP_FT_IMAGE=anidex-demo-app-faketime:local
DB_FT_IMAGE=anidex-demo-db-faketime:local
# "2026-10-15T12:00:00Z" -> FAKETIME "@2026-10-15 12:00:00" (start there, then tick).
DEMO_NOW=$(sed -n 's/^ *"now": *"\([^"]*\)".*/\1/p' demo/demo.config.json)
[ -n "$DEMO_NOW" ] || { echo "demo/demo.config.json has no \"now\"" >&2; exit 1; }
DEMO_FAKETIME="@$(printf '%s' "$DEMO_NOW" | sed 's/T/ /; s/Z$//')"
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
# libfaketime at one path in both images; the stack preloads it (FT_ENV below).
printf '%s\n' "FROM $APP_IMAGE" "USER root" \
  "RUN apt-get update && apt-get install -y --no-install-recommends libfaketime && rm -rf /var/lib/apt/lists/* && ln -s \"\$(dpkg -L libfaketime | grep '/libfaketimeMT.so.1\$')\" /usr/local/lib/faketime.so" \
  "USER appuser" | $ENGINE build -q -t "$APP_FT_IMAGE" -f - "$LOGS" >/dev/null
printf '%s\n' "FROM $PG_IMAGE" \
  "RUN apk add --no-cache libfaketime && ln -s /usr/lib/faketime/libfaketime.so.1 /usr/local/lib/faketime.so" \
  | $ENGINE build -q -t "$DB_FT_IMAGE" -f - "$LOGS" >/dev/null

say "record (sealed network)"
# One internal network: no container can reach anything outside it, and no port
# is published. The app runs with every outbound integration off (ANILIST_MOCK=1,
# ANIDEX_DISABLE_SCHEDULER=1); if it still calls out, the call fails here and the
# recorder fails on any failed request.
status=0
P=anidex-demo-$$
NET=$P-net
DB_ENV="-e POSTGRES_DB=anidex_demo -e POSTGRES_USER=anidex_demo -e POSTGRES_PASSWORD=demo-only-not-a-secret"
DB_URL=postgresql://anidex_demo:demo-only-not-a-secret@db:5432/anidex_demo
FT_ENV="-e LD_PRELOAD=/usr/local/lib/faketime.so -e DONT_FAKE_MONOTONIC=1"
cleanup() {
  $ENGINE logs "$P-app" >"$LOGS/app.log" 2>&1 || true
  $ENGINE rm -f "$P-recorder" "$P-app" "$P-db" >/dev/null 2>&1 || true
  $ENGINE network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM
$ENGINE network create --internal "$NET" >/dev/null
# shellcheck disable=SC2086
$ENGINE run -d --name "$P-db" --network "$NET" --network-alias db $DB_ENV $FT_ENV -e FAKETIME="$DEMO_FAKETIME" \
  -v "$ROOT/schema.sql:/docker-entrypoint-initdb.d/01-schema.sql:ro,z" "$DB_FT_IMAGE" >/dev/null
i=0
# TCP, not the Unix socket: the entrypoint's temporary init server (socket only)
# answers while schema.sql is still loading; TCP opens only on the final server.
until $ENGINE exec "$P-db" pg_isready -q -h 127.0.0.1 -U anidex_demo -d anidex_demo 2>/dev/null; do
  i=$((i + 1)); [ "$i" -gt 60 ] && { echo "ERROR db not ready"; exit 1; }; sleep 1
done
# shellcheck disable=SC2086
$ENGINE run -d --name "$P-app" --network "$NET" --network-alias anidex $FT_ENV -e FAKETIME="$DEMO_FAKETIME" \
  -e DATABASE_URL="$DB_URL" \
  -e SESSION_SECRET_KEY=demo-only-not-for-real-use \
  -e SETTINGS_ENCRYPTION_KEY=8CgnHDlfBgM3RQ9L0M_tjLWCtET3Cv8YdauDtn0bkfc= \
  -e TZ=UTC -e ANILIST_MOCK=1 -e ANIDEX_DISABLE_SCHEDULER=1 -e HEAVY_CORES="${HEAVY_CORES:-}" \
  -v "$ROOT/demo/covers:/app/app/static/demo-covers:ro,z" \
  "$APP_FT_IMAGE" sh -c 'exec ${HEAVY_CORES:+taskset -c "$HEAVY_CORES"} uvicorn app.main:app --host 0.0.0.0 --port 8888' >/dev/null
record $ENGINE run --rm --name "$P-recorder" --network "$NET" -w /work \
  -e APP_URL=http://anidex:8888 -e DATABASE_URL="$DB_URL" -e OUT=/work/_site -e HEAVY_CORES="${HEAVY_CORES:-}" \
  -e DEMO_SHIM_DIR=/kit-shim -v "$DEMO_KIT/shim:/kit-shim:ro,z" \
  -v "$ROOT:/work:z" "$PW_IMAGE" \
  sh -c 'exec ${HEAVY_CORES:+taskset -c "$HEAVY_CORES"} node demo/record.mjs'

if [ "$status" -ne 0 ] || grep -q '^ERROR' "$LOGS/record.log"; then
  echo "demo build FAILED (see $LOGS/record.log, $LOGS/app.log)" >&2
  exit 1
fi
[ -s "$ROOT/_site/index.html" ] && [ -s "$ROOT/_site/fixtures.json" ] || {
  echo "demo build FAILED: _site/index.html or _site/fixtures.json missing" >&2
  exit 1
}
say "done: $(grep '^RECORDED' "$LOGS/record.log")"
