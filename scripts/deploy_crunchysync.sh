#!/bin/bash
# deploy_crunchysync.sh — pull latest crunchysync image from GHCR

set -euo pipefail

IMAGE=ghcr.io/napandee/anime-tracker-crunchysync:latest

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "Pulling crunchysync image..."
docker pull "$IMAGE"

log "Done — $IMAGE ready. Run with:"
log "  docker run --rm --env-file ***REDACTED-PATH***/anime-tracker/.env $IMAGE"
