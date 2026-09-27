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
