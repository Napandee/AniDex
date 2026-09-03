# Upgrading and running migrations

This page is for whoever runs an AniDex instance — deploying new code and applying a
database schema change are two separate, deliberately manual steps here, not one
automated "upgrade" button.

## The code side is automatic

Pushing to `main` builds a new image and a self-hosted runner pulls it and restarts
the container — no admin action needed for a code-only change. The restart now
includes a post-deploy health check: if the new container doesn't come up healthy
within the check window, the deploy job automatically rolls back to the previous
working image and fails loudly (the workflow shows red) rather than leaving a
broken deploy silently running. The container itself also runs hardened by
default — non-root user, read-only root filesystem, dropped capabilities — no
admin action needed for any of this either. See the main [README](../../README.md)'s
deploy section for the full pipeline.

## The database side is not

Schema changes live in `migrations/` as numbered SQL files
(`001_add_multi_user.sql`, `002_backfill_and_tighten.sql`, ...). Nothing in the
deploy pipeline applies these — they're run by hand against the live Postgres,
deliberately kept separate from the code deploy so a schema change is never
silently rushed out alongside a routine code push. A CI check does fail a pull
request outright if it adds a new `migrations/` file without also bumping the
`LATEST_MIGRATION` marker in the same diff — that catches the mistake at
review time, but applying the migration to a real running instance is still a
separate, manual step described below.

### How to tell if you need to run one

A sticky banner appears at the top of every admin-facing page (not just Instance
Health) when the deployed code expects a migration that hasn't been applied to
this database yet — that's the authoritative, hard-to-miss signal now. **Admin →
Instance Health** still has the detail view if you want the exact numbers. Both
compare the highest migration number the running code was built against to a
marker stored in this database, and flag the gap if they're out of sync.

### How to apply one

Use the helper script instead of a raw `psql` invocation where possible — it takes
a full `pg_dump` backup automatically before touching anything, applies the
migration file, **and** updates the tracking marker the drift banner and Instance
Health read, all in one step, so neither the backup nor the marker can be
forgotten:

```bash
scripts/mark_migration_applied.sh 44
```

Useful flags: `--dry-run` validates the migration number/file resolve correctly
without touching the database at all (nothing applied, no backup taken);
`--force-no-backup` skips the automatic backup if you've already got one you trust
(you'll get a loud warning either way, since skipping it means nothing to restore
from if the migration goes wrong). The automatic backup lands in `./migration_backups/`
by default — override with the `BACKUP_DIR` env var — and is a raw schema+data
`pg_dump` of the whole database, the right tool for "the migration itself went
wrong." For the separate concern of restoring a full instance from one of the
scheduled `instance_backups` exports (Admin → Backups) instead, see
`scripts/restore_backup.sh`.

This assumes you have shell access to the host running the `anidex-postgres`
container (or equivalent — override via `POSTGRES_CONTAINER`/`POSTGRES_USER`/
`POSTGRES_DB` env vars if your setup differs).

If you'd rather apply a migration file directly (the script above is just this,
plus the marker bump):

```bash
docker exec -i <postgres-container> psql -U <db-user> -d <db-name> < migrations/00N_*.sql
```

**One migration is an exception to that generic command:**
`002_backfill_and_tighten.sql` needs a variable passed in, not a plain stdin pipe —
it backfills every pre-multi-user row to the instance owner's `user_id` via a
`:owner_id` psql variable used throughout the file:

```bash
psql -U <db-user> -d <db-name> -v owner_id=1 -f migrations/002_backfill_and_tighten.sql
```

(where `1` is your own account's real user id — piping the file through stdin the
normal way leaves `:owner_id` unresolved). It also has its own prerequisites — read
the file's own header comment before running it.

### Migrations are applied in order, and can't be skipped

If your instance is more than one migration behind, apply them in numeric order,
oldest first — each one assumes every migration before it has already run.
