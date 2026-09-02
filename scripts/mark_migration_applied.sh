#!/usr/bin/env bash
# Applies migrations/00N_*.sql to the live database AND bumps migration_state's
# marker (issue #380) in one command, so the marker can't be silently forgotten
# the way migration 028's own application was — that's the exact incident this
# whole feature exists to catch. Run from a repo checkout with access to the
# live Postgres container, same as any other manual migration apply per
# CLAUDE.md's "Schema migrations are manual" guardrail.
#
# Issue #469 — a pre-migration pg_dump is now taken by default before every
# apply (the safety net used to depend on the operator remembering a separate
# manual backup step; it no longer does). Pair this with restore_backup.sh for
# the app-data side of disaster recovery — this script's own backup is a raw
# schema+data pg_dump, the right tool for "the migration itself went wrong."
#
# Usage: scripts/mark_migration_applied.sh [--dry-run] [--force-no-backup] <N>
#   e.g. scripts/mark_migration_applied.sh 35
#   scripts/mark_migration_applied.sh --dry-run 35   # validate only, touches nothing
#
# Override container/DB name/user via env vars if this isn't run against the
# default live instance layout documented in CLAUDE.local.md. BACKUP_DIR
# overrides where the pre-migration pg_dump is written (default: ./migration_backups).
set -euo pipefail

DRY_RUN=0
FORCE_NO_BACKUP=0
N=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --force-no-backup) FORCE_NO_BACKUP=1 ;;
    -*)
      echo "Unknown flag: $arg" >&2
      exit 1
      ;;
    *)
      if [ -n "$N" ]; then
        echo "Usage: $0 [--dry-run] [--force-no-backup] <migration-number>" >&2
        exit 1
      fi
      N="$arg"
      ;;
  esac
done

if [ -z "$N" ]; then
  echo "Usage: $0 [--dry-run] [--force-no-backup] <migration-number>" >&2
  exit 1
fi

CONTAINER="${POSTGRES_CONTAINER:-anidex-postgres}"
DB_USER="${POSTGRES_USER:-anime_tracker}"
DB_NAME="${POSTGRES_DB:-anime_tracker}"
BACKUP_DIR="${BACKUP_DIR:-./migration_backups}"

if ! [[ "$N" =~ ^[0-9]+$ ]]; then
  echo "Migration number must be a plain integer (e.g. 35, not 035 or v35) — got '$N'" >&2
  exit 1
fi
# Migration files are always zero-padded to 3 digits (035_*.sql, not 35_*.sql) —
# this script's own usage example ("mark_migration_applied.sh 35") never actually
# matched a real file before this padding, since `migrations/35_*.sql` isn't a
# prefix match against `035_...`. Confirmed live: this script could not have ever
# successfully applied a real migration as documented.
#
# `10#$N` forces base-10 interpretation before padding — plain `printf "%03d" "$N"`
# treats a leading-zero input (e.g. someone passing "043" instead of "43") as octal
# in bash's arithmetic context, silently resolving to a DIFFERENT migration number
# (043 -> decimal 35). Found via #469's own --dry-run testing; exactly the class of
# silent-wrong-migration failure this script exists to prevent.
N_PADDED=$(printf "%03d" "$((10#$N))")

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

if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] Would back up $DB_NAME (unless --force-no-backup), apply $FILE, and bump migration_state to $N."
  echo "[dry-run] Nothing was touched — database untouched, no file was applied."
  exit 0
fi

if [ "$FORCE_NO_BACKUP" -eq 1 ]; then
  echo "WARNING: --force-no-backup set — applying migration $N to $DB_NAME with NO pre-migration backup. If this goes wrong, there is nothing to restore from." >&2
else
  mkdir -p "$BACKUP_DIR"
  BACKUP_FILE="$BACKUP_DIR/pre_migration_${N_PADDED}_$(date -u +%Y%m%dT%H%M%SZ).sql"
  echo "Backing up $DB_NAME to $BACKUP_FILE before applying migration $N..."
  docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" > "$BACKUP_FILE"
  echo "Backup written ($(du -h "$BACKUP_FILE" | cut -f1))."
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
