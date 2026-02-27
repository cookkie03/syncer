#!/bin/sh
set -e

CRONTAB_FILE="/tmp/notion-backup.cron"

: "${NOTION_API_TOKEN:?NOTION_API_TOKEN is required}"

# Default: daily at 02:00 UTC. Override with BACKUP_SCHEDULE (cron syntax).
BACKUP_SCHEDULE="${BACKUP_SCHEDULE:-0 2 * * *}"

echo "[entrypoint] Running initial backup..."
python3 /app/backup.py || echo "[entrypoint] WARNING: initial backup failed — will retry on schedule"

echo "${BACKUP_SCHEDULE} python3 /app/backup.py 2>&1" > "$CRONTAB_FILE"
echo "[entrypoint] Scheduling backup '${BACKUP_SCHEDULE}' via supercronic"
exec supercronic "$CRONTAB_FILE"
