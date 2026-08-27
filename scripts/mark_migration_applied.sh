#!/usr/bin/env bash
# Applies migrations/00N_*.sql to the live database AND bumps migration_state's
# marker (issue #380) in one command, so the marker can't be silently forgotten
# the way migration 028's own application was — that's the exact incident this
# whole feature exists to catch. Run from a repo checkout with access to the
# live Postgres container, same as any other manual migration apply per
# CLAUDE.md's "Schema migrations are manual" guardrail.
#
# Usage: scripts/mark_migration_applied.sh <N>
#   e.g. scripts/mark_migration_applied.sh 35
#
# Override container/DB name/user via env vars if this isn't run against the
# default live instance layout documented in CLAUDE.local.md.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <migration-number>" >&2
  exit 1
fi

N="$1"
CONTAINER="${POSTGRES_CONTAINER:-anidex-postgres}"
DB_USER="${POSTGRES_USER:-anime_tracker}"
DB_NAME="${POSTGRES_DB:-anime_tracker}"

FILE=$(ls migrations/${N}_*.sql 2>/dev/null | head -1 || true)
if [ -z "$FILE" ]; then
  echo "No migration file found for number $N in migrations/" >&2
  exit 1
fi

echo "Applying $FILE to $DB_NAME on $CONTAINER..."
docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" < "$FILE"

echo "Bumping migration_state marker to $N..."
docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c "
INSERT INTO migration_state (id, highest_applied_migration, updated_at)
VALUES (1, $N, now())
ON CONFLICT (id) DO UPDATE SET highest_applied_migration = EXCLUDED.highest_applied_migration, updated_at = now();
"

echo "Done — migration $N applied and marked on $DB_NAME."
