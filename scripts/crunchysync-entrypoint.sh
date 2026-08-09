#!/bin/bash
set -euo pipefail

cat > /app/config.yaml <<EOF
crunchyroll:
  etp_rt: "${CRUNCHYROLL_ETP_RT}"

exporters:
  anilist:
    client_id: "${ANILIST_CLIENT_ID}"
    access_token: "${ANILIST_TOKEN}"
EOF

exec python src/main.py sync --target anilist
