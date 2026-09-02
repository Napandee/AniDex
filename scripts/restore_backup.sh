#!/usr/bin/env bash
# Issue #469 — thin entry point for restore_backup.py (kept a .sh alongside
# mark_migration_applied.sh so both DR scripts live at the same path shape;
# the actual restore logic is Python since it's parsing/upserting JSON, not
# shelling out to psql line by line).
#
# Usage: scripts/restore_backup.sh --zip path/to/backup.zip [--dry-run]
#   DATABASE_URL must point at the target database (same convention every
#   other scripts/*.py uses — see scripts/run_full_sync.py).
set -euo pipefail

exec python3 "$(dirname "${BASH_SOURCE[0]}")/restore_backup.py" "$@"
