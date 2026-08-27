# Upgrading and running migrations

This page is for whoever runs an AniDex instance — deploying new code and applying a
database schema change are two separate, deliberately manual steps here, not one
automated "upgrade" button.

## The code side is automatic

Pushing to `main` builds a new image and a self-hosted runner pulls it and restarts
the container — no admin action needed for a code-only change. See the main
[README](../../README.md)'s deploy section for the full pipeline.

## The database side is not

Schema changes live in `migrations/` as numbered SQL files
(`001_add_multi_user.sql`, `002_backfill_and_tighten.sql`, ...). Nothing in the
deploy pipeline applies these — they're run by hand against the live Postgres,
deliberately kept separate from the code deploy so a schema change is never
silently rushed out alongside a routine code push.

### How to tell if you need to run one

**Admin → Instance Health** shows a warning when the deployed code expects a
migration that hasn't been applied to this database yet — check there first, it's
the authoritative signal. It compares the highest migration number the running
code was built against to a marker stored in this database, and flags the gap if
they're out of sync.

### How to apply one

Use the helper script instead of a raw `psql` invocation where possible — it applies
the migration file **and** updates the tracking marker Instance Health reads, in one
step, so the marker can't be forgotten:

```bash
scripts/mark_migration_applied.sh 35
```

This assumes you have shell access to the host running the `anidex-postgres`
container (or equivalent — override via `POSTGRES_CONTAINER`/`POSTGRES_USER`/
`POSTGRES_DB` env vars if your setup differs). Always back up your database before
running any migration against real data.

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
