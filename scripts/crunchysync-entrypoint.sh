#!/bin/bash
set -euo pipefail

log() { echo "[crunchysync] $*"; }

# Generate crunchyexporter-cli config from env vars
mkdir -p /crunchyexporter/data
cat > /crunchyexporter/config.yaml <<EOF
crunchyroll:
  etp_rt: "${CRUNCHYROLL_ETP_RT}"
EOF

log "Fetching Crunchyroll watch history..."
cd /crunchyexporter
python src/main.py fetch

log "Running AniList + Postgres sync..."
HISTORY_PATH=/crunchyexporter/data/history.json python /app/scripts/sync_crunchyroll.py
