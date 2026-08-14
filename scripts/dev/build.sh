#!/usr/bin/env bash
# Builds the local dev/test image. Safe to run any time, including by an
# assistant/agent — contains no credentials, only code and dependencies.
set -euo pipefail
cd "$(dirname "$0")/../.."
podman build -t anidex-sync-test -f scripts/dev/Containerfile .
