#!/bin/bash
# deploy_crunchysync.sh — pull latest code and rebuild the crunchysync image
# Run manually or triggered by the same GitHub webhook as the main app.

set -euo pipefail

REPO_DIR=***REDACTED-PATH***/anime-tracker/repo
IMAGE=anime-tracker-crunchysync

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "Pulling latest code..."
cd "$REPO_DIR"
git pull origin main

log "Building crunchysync image..."
docker build -f Dockerfile.crunchysync -t "$IMAGE" .

log "Done — $IMAGE built. Run with:"
log "  docker run --rm --env-file ***REDACTED-PATH***/anime-tracker/.env $IMAGE"
