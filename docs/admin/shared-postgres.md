# Running AniDex against a shared/external Postgres instance

`compose/anidex.yml` (the app) and `compose/anidex-postgres.yml` (a dedicated bundled
Postgres) are two independent compose files — nothing couples them together. If you
already run a Postgres instance for other apps (Unraid's community-app Postgres,
a managed Postgres, a Postgres on a NAS, etc.) and don't want a second dedicated
instance just for AniDex, you can point AniDex at your existing instance instead.
This is a fully supported topology, not a workaround.

This page was written after a real test of exactly this setup (issue #496): a scratch
Postgres instance was provisioned with a second, unrelated database on it (simulating a
genuinely shared/multi-tenant server), AniDex's own database and role were created
alongside it, `schema.sql` and a real migration were applied, and the app was built and
run against it end to end — login, an authenticated route, a CSRF-protected POST write,
and the admin Instance Health panel all behaved identically to a bundled-Postgres
deployment. **No code changes were needed.** `app/db.py` reads `DATABASE_URL` as a plain
connection string with no assumption baked in about which Postgres instance, host, or
container it points to.

## Prerequisites

- A reachable Postgres **16 or newer**, network-reachable from wherever the AniDex app
  container runs (same Docker network, same host, or over the network with appropriate
  firewall/TLS as your setup requires)
- A **dedicated database and role** for AniDex within that instance — AniDex should own
  its own database, not share tables inside another app's database. It does not need
  superuser or instance-wide privileges, only ownership of its own database.

## 1. Create a dedicated database and role

Connect to your existing Postgres instance as an admin and run:

```sql
CREATE ROLE anime_tracker LOGIN PASSWORD 'a-real-password';
CREATE DATABASE anime_tracker OWNER anime_tracker;
```

(Names are just a convention matching the bundled compose file's defaults — use
whatever database/role names fit your existing instance's own naming scheme. Nothing in
the app or schema hardcodes these names.)

## 2. Apply the schema

For a brand-new AniDex install, `schema.sql` is the full target schema — it already
includes every table the app expects (equivalent to a base install plus every file
under `migrations/` applied in order). Apply it directly against your new database:

```bash
psql "postgresql://anime_tracker:a-real-password@your-shared-host:5432/anime_tracker" \
  -f schema.sql
```

or, if you'd rather not install `psql` locally, run it through a throwaway container the
same way the main [Quick start](../../README.md#quick-start) does:

```bash
docker run --rm \
  -v $(pwd)/schema.sql:/schema.sql \
  postgres:16-alpine \
  psql "postgresql://anime_tracker:a-real-password@your-shared-host:5432/anime_tracker" -f /schema.sql
```

This only touches the `anime_tracker` database you created in step 1 — it never
references or assumes anything about other databases/roles that might already exist on
the same shared instance.

**Migrating an already-running AniDex instance onto a shared instance instead of a
fresh install?** Restore your existing data (e.g. `pg_dump`/`pg_restore`, or AniDex's
own admin export/import) into the new database, then bring it up to date the normal way
— see [Upgrading and migrations](upgrading.md). The migration tooling
(`scripts/mark_migration_applied.sh`) already takes the Postgres container/user/database
name as overridable env vars (`POSTGRES_CONTAINER`, `POSTGRES_USER`, `POSTGRES_DB`) —
it was never hardcoded to the bundled `anidex-postgres` container name, so it works
unchanged against a differently-named, differently-provisioned instance:

```bash
POSTGRES_CONTAINER=your-shared-postgres-container \
POSTGRES_USER=anime_tracker \
POSTGRES_DB=anime_tracker \
  scripts/mark_migration_applied.sh 43
```

If your shared Postgres isn't running in a container you can `docker exec` into at all
(a managed/cloud Postgres, for instance), apply the migration file directly with `psql`
against its connection string instead, then update the marker by hand — see
[Upgrading and migrations](upgrading.md#how-to-apply-one) for the exact fallback
commands.

## 3. Run the app alone, without `anidex-postgres.yml`

Set `DATABASE_URL` in your `.env` to point at the shared instance and its database:

```
DATABASE_URL=postgresql://anime_tracker:a-real-password@your-shared-host:5432/anime_tracker
```

Then bring up only the app compose file — skip `compose/anidex-postgres.yml` entirely,
there is nothing to deploy for the database side:

```bash
docker compose -f compose/anidex.yml up -d
```

That's the whole difference from the default Quick Start path: one fewer compose file,
one Postgres connection string that happens to point somewhere else. Everything after
that — registering the first admin account, adding AniList credentials, sync,
scheduling, the admin panel — behaves exactly the same.

## What was verified, and what wasn't changed

Confirmed by real test, alongside a second unrelated fake database on the same
instance:

- `schema.sql` applies cleanly with no errors and touches only AniDex's own database
- The built app image boots and serves `/auth/login` correctly
- A real local-auth login, an authenticated library-page render, and a CSRF-protected
  `POST /settings/display` write all work and persist correctly
- **Admin → Instance Health** renders correctly and reports migration status the same
  way it would against the bundled instance — a fresh install has no
  `migration_state` row yet (schema.sql seeds none, by design — see the comment above
  `CREATE TABLE migration_state` in `schema.sql`), so it correctly shows "Unknown
  (marker not set yet)" rather than anything shared-instance-specific breaking
- `scripts/mark_migration_applied.sh`, using its existing `POSTGRES_CONTAINER`/
  `POSTGRES_USER`/`POSTGRES_DB` overrides, applies a real migration file and bumps the
  tracking marker against a non-default-named container
- The `pytest` suite passes unchanged with `DATABASE_URL` pointed at the shared
  instance

No changes were made to `app/db.py`, connection handling, or migration tooling — per
issue #496's scope, this page documents and formalizes something that already worked,
rather than introducing new functionality.

## What's still on you

Running Postgres yourself, shared or not, means you own its operational concerns that
the bundled `compose/anidex-postgres.yml` would otherwise hand you a starting point for:
backups, version upgrades, and access control on the instance itself. AniDex's own
export/import (Admin panel) and `instance_backups`-based scheduled backups (see
[Upgrading and migrations](upgrading.md)) cover AniDex's data, but the underlying
Postgres instance's own backup/HA story is whatever you've already set up for the rest
of what runs on it.
