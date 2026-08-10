#!/bin/bash
# deploy.sh — pull latest code, rebuild image, restart container
# Runs on Unraid via n8n SSH step after a push to main.

set -euo pipefail

REPO_DIR=***REDACTED-PATH***/anime-tracker/repo
ENV_FILE=***REDACTED-PATH***/anime-tracker/.env
CONTAINER=anime-tracker
IMAGE=anime-tracker
PORT=8889

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "Pulling latest code..."
cd "$REPO_DIR"
git fetch origin main
git reset --hard origin/main

log "Building image..."
docker build -t "$IMAGE" .

log "Replacing container..."
docker rm -f "$CONTAINER" 2>/dev/null || true

docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -p "$PORT":8888 \
  --env-file "$ENV_FILE" \
  "$IMAGE"

log "Done — $CONTAINER up on port $PORT"
