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

if ! [[ "$N" =~ ^[0-9]+$ ]]; then
  echo "Migration number must be a plain integer (e.g. 35, not 035 or v35) — got '$N'" >&2
  exit 1
fi
# Migration files are always zero-padded to 3 digits (035_*.sql, not 35_*.sql) —
# this script's own usage example ("mark_migration_applied.sh 35") never actually
# matched a real file before this padding, since `migrations/35_*.sql` isn't a
# prefix match against `035_...`. Confirmed live: this script could not have ever
# successfully applied a real migration as documented.
N_PADDED=$(printf "%03d" "$N")

# Issue: this used to be `ls migrations/${N}_*.sql | head -1`, which silently
# applied only the alphabetically-first match if two files ever claimed the same
# number — exactly what happened with migrations 008/009/028 (see e.g. migration
# 041's header for the real incident this caused: plex_sync_state went unapplied
# on prod for days because a duplicate 028 file was picked over it). Fail loudly
# on any ambiguity instead of guessing.
MATCHES=$(ls migrations/${N_PADDED}_*.sql 2>/dev/null || true)
MATCH_COUNT=$(echo "$MATCHES" | grep -c . || true)
if [ "$MATCH_COUNT" -eq 0 ]; then
  echo "No migration file found for number $N in migrations/" >&2
  exit 1
elif [ "$MATCH_COUNT" -gt 1 ]; then
  echo "Ambiguous — more than one migration file claims number $N:" >&2
  echo "$MATCHES" >&2
  echo "Renumber one of them before running this script." >&2
  exit 1
fi
FILE="$MATCHES"

echo "Applying $FILE to $DB_NAME on $CONTAINER..."
docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" < "$FILE"

echo "Bumping migration_state marker to $N..."
docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c "
INSERT INTO migration_state (id, highest_applied_migration, updated_at)
VALUES (1, $N, now())
ON CONFLICT (id) DO UPDATE SET highest_applied_migration = EXCLUDED.highest_applied_migration, updated_at = now();
"

echo "Done — migration $N applied and marked on $DB_NAME."
