# Database backup and restore

Nightly Postgres backups for the production stack: a `db-backup` container
(`compose.backup.yml`) dumps the database at 02:30 IST, verifies the archive,
prunes dumps older than 14 days, and posts to the owner's phone if anything
fails. Dumps live **outside** the Docker volume, in a host directory
(`/home/krishna/kite-algo-backups` by default), so losing the `postgres_data`
volume does not lose the backups.

The service is `db-backup` (container `kite-db-backup`); the script it runs is
`scripts/ops/pg_backup.sh`. Compose files below are read from the deployment
checkout (the same directory the production stack is run from). This runbook
never prints `.env`: every credential it needs (`$DB_USER`, `$DB_NAME`) must be
exported by the operator into the current shell from their own environment.

## 1. Start the service and check it

Start it alongside the running stack (postgres must already be healthy):

```bash
docker compose -f compose.yml -f compose.backup.yml up -d db-backup
```

Check the container and the most recent successful dump:

```bash
docker logs kite-db-backup --tail 20
cat /home/krishna/kite-algo-backups/LAST_SUCCESS
```

`LAST_SUCCESS` holds `<ISO-8601 UTC timestamp> <dump file name>` and is the
quickest "is the backup working?" signal. The dump named there is in the same
directory.

The container dumps once per day at 02:30 IST and then sleeps until the next
02:30 IST. To also dump immediately when the container starts, either set
`BACKUP_RUN_ON_START=1` in the environment or do a manual backup (below).

## 2. Take a manual backup now

Run a one-shot backup in a throwaway container (does not disturb the running
service):

```bash
docker compose -f compose.yml -f compose.backup.yml run --rm -e BACKUP_ONCE=1 db-backup
```

It writes `kite-YYYYmmdd-HHMM.dump` into the backup directory, refreshes
`LAST_SUCCESS`, and prints `backup: ok ...` on success.

## 3. Verify a dump without touching production

Restore a dump into a scratch database on the **same** server. This reads the
dump and creates a temporary database; it does not modify production.

```bash
# Export the deploy's user name into this shell from your own environment.
export DB_USER=<your deployment DB user>

docker exec kite-postgres createdb -U "$DB_USER" kite_restore_check
docker exec -i kite-postgres pg_restore -U "$DB_USER" -d kite_restore_check --no-owner \
  < /home/krishna/kite-algo-backups/kite-<stamp>.dump

# Spot-check that the restored schema carries real data.
docker exec kite-postgres psql -U "$DB_USER" -d kite_restore_check \
  -c 'SELECT count(*) FROM alembic_version;'

# Tear the scratch database down.
docker exec kite-postgres dropdb -U "$DB_USER" kite_restore_check
```

A non-zero `pg_restore` exit or an empty/erroring `alembic_version` read means
the dump is not trustworthy. `kite-<stamp>.dump` is whichever file
`LAST_SUCCESS` names.

## 4. Full restore (disaster)

**Owner only, destructive.** This drops and rebuilds the production database.
Stop the app first so nothing is holding a connection.

```bash
# Export both names from your own environment.
export DB_USER=<your deployment DB user>
export DB_NAME=<your deployment DB name>

# a. Stop the app services (leave postgres running).
docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml \
  stop finance-app alerts-worker strategy-runner market-runtime

# b. Drop and recreate the production database.
docker exec kite-postgres dropdb -U "$DB_USER" --force "$DB_NAME"
docker exec kite-postgres createdb -U "$DB_USER" "$DB_NAME"

# c. Restore the chosen dump.
docker exec -i kite-postgres pg_restore -U "$DB_USER" -d "$DB_NAME" --no-owner \
  < /home/krishna/kite-algo-backups/kite-<stamp>.dump

# d. Start finance-app first: its entrypoint runs Alembic migrations.
docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml \
  up -d finance-app
# Wait until finance-app reports healthy, then bring the rest back.
docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml \
  up -d market-runtime alerts-worker strategy-runner frontend-next
```

Bring `finance-app` up before the others because it owns the migrations
(`documents/runbooks/README.md`). The dump already carries the migrated schema,
so Alembic should be a no-op; starting it first still keeps the app consistent
with the restored schema before any worker starts.

## 5. Copy the backups off the machine

The backup directory sits on the same host as the database, so it is not a
disaster story on its own. Sync `/home/krishna/kite-algo-backups` to another
disk or a cloud/off-site location with whatever tooling the owner prefers
(`rsync`, `rclone`, ...); no such tooling is built here.
