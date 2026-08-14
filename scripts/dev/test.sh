#!/usr/bin/env bash
# Runs the pytest smoke suite (tests/) inside the same container image used for the
# live-credential probes — no secrets involved, safe to run any time.
set -euo pipefail
cd "$(dirname "$0")/../.."
podman image exists anidex-sync-test || scripts/dev/build.sh
podman run --rm anidex-sync-test python3 -m pytest tests/ -v
