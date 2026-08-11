#!/bin/bash
# deploy_crunchysync.sh — pull latest crunchysync image from GHCR.
#
# Usage: ENV_FILE=/path/to/.env OWNER=yourghcruser ./deploy_crunchysync.sh

set -euo pipefail

ENV_FILE=${ENV_FILE:-".env"}
OWNER=${OWNER:-"$(git remote get-url origin | sed 's|.*github.com/||;s|/.*||')"}
IMAGE=ghcr.io/${OWNER}/anime-tracker-crunchysync:latest

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "Authenticating with GHCR..."
GHCR_TOKEN=$(grep '^GHCR_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
echo "$GHCR_TOKEN" | docker login ghcr.io -u "$OWNER" --password-stdin

log "Pulling crunchysync image..."
docker pull "$IMAGE"

log "Done — $IMAGE ready. Run with:"
log "  docker run --rm --env-file $ENV_FILE $IMAGE"
