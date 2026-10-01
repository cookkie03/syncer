#!/bin/bash
set -euo pipefail
umask 077
: "${TODOIST_API_TOKEN:?TODOIST_API_TOKEN is required}"
export TODOIST_STATE_DIR="${TODOIST_STATE_DIR:-${TODOIST_DATA_DIR:-/state}}"
export TODOIST_BACKUP_DIR="${TODOIST_BACKUP_DIR:-/backup}"
export TODOIST_LEGACY_DIR="${TODOIST_LEGACY_DIR:-/legacy-data}"
export TZ="${TZ:-Europe/Amsterdam}"
LOG_DIR="${LOG_DIR:-/logs}"
mkdir -p "$LOG_DIR" "$TODOIST_STATE_DIR" "$TODOIST_BACKUP_DIR"
LOG_FILE="$LOG_DIR/todoist-backup.log"
BACKUP_SCHEDULE="${TODOIST_BACKUP_SCHEDULE:-0 */4 * * *}"
CRONTAB_FILE="${TODOIST_CRONTAB_FILE:-/tmp/todoist-backup.cron}"
echo "[entrypoint] Running initial Todoist backup..." | tee -a "$LOG_FILE"
python /app/backup.py backup 2>&1 | tee -a "$LOG_FILE" || \
  echo "[entrypoint] Initial backup failed; retrying on schedule" | tee -a "$LOG_FILE"
printf '%s python /app/backup.py backup\n' "$BACKUP_SCHEDULE" > "$CRONTAB_FILE"
echo "[entrypoint] Scheduling Todoist backup: $BACKUP_SCHEDULE ($TZ)" | tee -a "$LOG_FILE"
exec supercronic "$CRONTAB_FILE" >> "$LOG_FILE" 2>&1
