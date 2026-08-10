#!/bin/bash
# deploy_crunchysync.sh — pull latest crunchysync image from GHCR

set -euo pipefail

IMAGE=ghcr.io/napandee/anime-tracker-crunchysync:latest
ENV_FILE=***REDACTED-PATH***/anime-tracker/.env

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "Authenticating with GHCR..."
GHCR_TOKEN=$(grep '^GHCR_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
echo "$GHCR_TOKEN" | docker login ghcr.io -u napandee --password-stdin

log "Pulling crunchysync image..."
docker pull "$IMAGE"

log "Done — $IMAGE ready. Run with:"
log "  docker run --rm --env-file ***REDACTED-PATH***/anime-tracker/.env $IMAGE"
