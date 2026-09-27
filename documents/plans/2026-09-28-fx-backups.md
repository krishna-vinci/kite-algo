# Nightly Postgres Backups Implementation Plan

> **For agentic workers:** Execute task-by-task, in order. Do not load brainstorming or other workflow skills. If a step does not fit the real files, stop and report the mismatch instead of improvising.

**Goal:** Every night the production database is dumped, verified readable, kept for 14 days outside the Docker volume, and a failure pings the owner's phone. A documented, tested restore procedure exists.

**Architecture:** A `db-backup` service in a new `compose.backup.yml` (image `postgres:16-alpine`, same as the DB) runs `scripts/ops/pg_backup.sh` in a loop: at 02:30 IST it runs `pg_dump -Fc` against `postgres:5432`, writes `kite-YYYYmmdd-HHMM.dump` into a host directory bind-mounted at `/backups`, verifies it with `pg_restore --list`, deletes dumps older than 14 days, and on any failure POSTs a message to the ntfy URL. Credentials come from the same `${DB_USER}/${DB_PASSWORD}/${DB_NAME}` compose interpolation the `postgres` service uses — the plan never reads or edits `.env`.

**Tech Stack:** POSIX sh, postgres 16 client tools, docker compose.

**Spec:** Owner decision 2026-09-28 (backups); Explore facts (compose.yml:2-30 postgres service; no existing backup tooling).

## Global Constraints

- Worktree `/home/krishna/kite-algo-worktrees/fx-backups`, branch `codex/fx-backups`. Read `AGENTS.md` first.
- Never read, print or edit `.env*`. Never print `DB_PASSWORD` or the ntfy URL in logs.
- Do NOT run `docker compose up` for the real stack and do NOT touch the running `kite-postgres` container. Test only with the disposable server at `127.0.0.1:15433` (user `postgres`, password `testonly`) and a temp directory.
- Do NOT commit. "Checkpoint" = `git status`.

## Decisions (fixed)

1. Host backup directory: `${BACKUP_DIR:-/home/krishna/kite-algo-backups}` bind-mounted to `/backups`.
2. Schedule: daily at `${BACKUP_AT_IST:-02:30}` IST; the loop sleeps until the next occurrence (computed with `TZ=Asia/Kolkata date`). `BACKUP_RUN_ON_START=1` additionally runs one dump at container start (default `0`).
3. Retention: `${BACKUP_KEEP_DAYS:-14}` days, by file mtime, only files matching `kite-*.dump`.
4. Verification: `pg_restore --list <file> > /dev/null` must succeed and the file must be > 1 KiB; otherwise the dump is renamed `*.dump.bad` and the failure alert is sent.
5. Alert: `curl -fsS -m 10 -H "Title: DB BACKUP FAILED" -d "<reason>" "$NTFY_URL"` when `NTFY_URL` is non-empty; the compose service passes `NTFY_URL: ${SCHEDULER_NTFY_URL:-}`. The postgres alpine image has no curl → use `wget -q -O- --header "Title: DB BACKUP FAILED" --post-data "<reason>" "$NTFY_URL"` (busybox wget supports these flags; verify in Task 1 Step 3).
6. Success writes `/backups/LAST_SUCCESS` containing the ISO timestamp and file name (lets status checks read backup age later).

## File Structure

- Create `scripts/ops/pg_backup.sh` (executable).
- Create `compose.backup.yml`.
- Create `documents/runbooks/backup-restore.md`.
- Create `tests/ops/test_pg_backup_script.py` (a pytest that runs the script once against the 15433 test server).

---

### Task 1: The backup script

- [ ] **Step 1: Write `scripts/ops/pg_backup.sh`:**

```sh
#!/bin/sh
# Nightly pg_dump with verification, retention and a failure alert.
# Env: PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE (standard libpq names),
#      BACKUP_DIR (default /backups), BACKUP_KEEP_DAYS (14), BACKUP_AT_IST (02:30),
#      BACKUP_RUN_ON_START (0), BACKUP_ONCE (0: loop forever; 1: one dump then exit), NTFY_URL.
set -u
BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
AT="${BACKUP_AT_IST:-02:30}"

alert() {
  [ -n "${NTFY_URL:-}" ] || return 0
  wget -q -O- --header "Title: DB BACKUP FAILED" --post-data "$1" "$NTFY_URL" >/dev/null 2>&1 || true
}

backup_once() {
  stamp="$(TZ=Asia/Kolkata date +%Y%m%d-%H%M)"
  file="$BACKUP_DIR/kite-$stamp.dump"
  tmp="$file.partial"
  if ! pg_dump -Fc -f "$tmp"; then
    rm -f "$tmp"; alert "pg_dump failed at $stamp"; echo "backup: pg_dump failed" >&2; return 1
  fi
  size="$(wc -c < "$tmp")"
  if [ "$size" -le 1024 ] || ! pg_restore --list "$tmp" >/dev/null 2>&1; then
    mv "$tmp" "$file.bad"; alert "backup $stamp failed verification (size $size)"; echo "backup: verification failed" >&2; return 1
  fi
  mv "$tmp" "$file"
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(basename "$file")" > "$BACKUP_DIR/LAST_SUCCESS"
  find "$BACKUP_DIR" -maxdepth 1 -name 'kite-*.dump' -type f -mtime +"$KEEP_DAYS" -delete
  echo "backup: ok $(basename "$file") ($size bytes)"
}

seconds_until_next() {
  now="$(TZ=Asia/Kolkata date +%s)"
  today="$(TZ=Asia/Kolkata date +%Y-%m-%d)"
  target="$(TZ=Asia/Kolkata date -d "$today $AT" +%s 2>/dev/null)"
  [ -n "$target" ] || { echo 86400; return; }
  [ "$target" -le "$now" ] && target=$((target + 86400))
  echo $((target - now))
}

mkdir -p "$BACKUP_DIR"
if [ "${BACKUP_ONCE:-0}" = "1" ]; then backup_once; exit $?; fi
[ "${BACKUP_RUN_ON_START:-0}" = "1" ] && backup_once
while true; do
  sleep "$(seconds_until_next)"
  backup_once || true
done
```
  `chmod +x scripts/ops/pg_backup.sh`.

- [ ] **Step 2: Test** — create `tests/ops/test_pg_backup_script.py`:

```python
"""The backup script dumps, verifies and records success (disposable PG only)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ADMIN = "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "pg_backup.sh"


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")
def test_one_backup_is_written_and_verified(tmp_path):
    env = dict(os.environ, PGHOST="127.0.0.1", PGPORT="15433", PGUSER="postgres",
               PGPASSWORD="testonly", PGDATABASE="postgres", BACKUP_DIR=str(tmp_path),
               BACKUP_ONCE="1", NTFY_URL="")
    result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    dumps = list(tmp_path.glob("kite-*.dump"))
    assert len(dumps) == 1 and dumps[0].stat().st_size > 1024
    assert (tmp_path / "LAST_SUCCESS").read_text().strip().endswith(dumps[0].name)


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")
def test_a_failed_dump_leaves_no_dump_and_fails(tmp_path):
    env = dict(os.environ, PGHOST="127.0.0.1", PGPORT="1", PGUSER="postgres", PGPASSWORD="x",
               PGDATABASE="postgres", BACKUP_DIR=str(tmp_path), BACKUP_ONCE="1", NTFY_URL="")
    result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert list(tmp_path.glob("kite-*.dump")) == []
```
  If the host has no `pg_dump`, run the same script inside a throwaway container instead and report the command: `docker run --rm --network host -v "$PWD/scripts/ops:/s:ro" -v "$TMPDIR_PATH:/backups" -e PGHOST=127.0.0.1 -e PGPORT=15433 -e PGUSER=postgres -e PGPASSWORD=testonly -e PGDATABASE=postgres -e BACKUP_ONCE=1 postgres:16-alpine sh /s/pg_backup.sh` (use a temp dir). The version of `pg_dump` must be ≥ the server's (16).

- [ ] **Step 3: Run** `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/ops/test_pg_backup_script.py -q` → PASS (or the docker variant → a verified dump in the temp dir). Also verify busybox wget flags: `docker run --rm postgres:16-alpine wget --help 2>&1 | grep -E "post-data|header"` → both present; if not, report it.
- [ ] **Step 4: Checkpoint.**

---

### Task 2: Compose service

- [ ] Create `compose.backup.yml`:

```yaml
# Nightly database backups. Start with:
#   docker compose -f compose.yml -f compose.backup.yml up -d db-backup
services:
  db-backup:
    image: postgres:16-alpine
    container_name: kite-db-backup
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    entrypoint: ["sh", "/ops/pg_backup.sh"]
    environment:
      PGHOST: postgres
      PGPORT: "5432"
      PGUSER: ${DB_USER:-postgres}
      PGPASSWORD: ${DB_PASSWORD:-postgres}
      PGDATABASE: ${DB_NAME:-postgres}
      BACKUP_DIR: /backups
      BACKUP_KEEP_DAYS: ${BACKUP_KEEP_DAYS:-14}
      BACKUP_AT_IST: ${BACKUP_AT_IST:-02:30}
      BACKUP_RUN_ON_START: ${BACKUP_RUN_ON_START:-0}
      NTFY_URL: ${SCHEDULER_NTFY_URL:-}
    volumes:
      - ./scripts/ops:/ops:ro
      - ${BACKUP_DIR:-/home/krishna/kite-algo-backups}:/backups
```
- [ ] Validate syntax only: `docker compose -f compose.yml -f compose.backup.yml config --services` → output includes `db-backup` (this renders config; do NOT pipe the full `config` output anywhere — it contains interpolated secrets; only `--services`).
- [ ] **Checkpoint.**

---

### Task 3: Restore runbook

- [ ] Create `documents/runbooks/backup-restore.md` covering, with exact commands:
  1. Starting the service (command from the compose header) and checking it: `docker logs kite-db-backup --tail 20`, `cat /home/krishna/kite-algo-backups/LAST_SUCCESS`.
  2. A manual backup now: `docker compose -f compose.yml -f compose.backup.yml run --rm -e BACKUP_ONCE=1 db-backup`.
  3. **Verify a dump without touching production:** restore into a scratch database on the same server: `docker exec kite-postgres createdb -U "$DB_USER" kite_restore_check` then `docker exec -i kite-postgres pg_restore -U "$DB_USER" -d kite_restore_check --no-owner < <dump>`; spot-check `SELECT count(*) FROM alembic_version;`; then `dropdb kite_restore_check`. (Explain that `$DB_USER` must be exported in the shell from the operator's own environment — the runbook never prints `.env`.)
  4. **Full restore (disaster):** stop app services (`finance-app alerts-worker strategy-runner market-runtime`), `dropdb`/`createdb` the prod database, `pg_restore --no-owner -d <db> <dump>`, start `finance-app` first (it runs migrations), then the rest. Mark this section "owner only, destructive".
  5. Off-machine copy recommendation: sync `/home/krishna/kite-algo-backups` to another disk/cloud (one line, no tooling built).
- [ ] Add a one-line pointer in `documents/runbooks/README.md` to the new runbook.
- [ ] **Final report** per AGENTS.md (changed files, the exact test/validation commands and results). Remind the orchestrator that starting the service on the real stack is an owner action.
