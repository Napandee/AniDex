#!/bin/bash
# deploy.sh — manual fallback: pull latest image and restart the app container.
# Normal deployments are handled automatically by GitHub Actions (build-app.yml).
#
# Usage: ENV_FILE=/path/to/.env OWNER=yourghcruser ./deploy.sh

set -euo pipefail

ENV_FILE=${ENV_FILE:-".env"}
OWNER=${OWNER:-"$(git remote get-url origin | sed 's|.*github.com/||;s|/.*||')"}
CONTAINER=anidex
IMAGE=ghcr.io/${OWNER}/anidex:latest
PORT=8889

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "Authenticating with GHCR..."
GHCR_TOKEN=$(grep '^GHCR_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
echo "$GHCR_TOKEN" | docker login ghcr.io -u "$OWNER" --password-stdin

log "Pulling image..."
docker pull "$IMAGE"

log "Replacing container..."
docker stop "$CONTAINER" 2>/dev/null || true
docker rm   "$CONTAINER" 2>/dev/null || true

docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -p "$PORT":8888 \
  --env-file "$ENV_FILE" \
  "$IMAGE"

log "Done — $CONTAINER up on port $PORT"
