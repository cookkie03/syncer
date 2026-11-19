#!/bin/bash
set -euo pipefail

CRONTAB_FILE="/tmp/github-backup.cron"
LOG_DIR="${LOG_DIR:-/logs}"
mkdir -p "$LOG_DIR"

: "${GITHUB_TOKEN:?GITHUB_TOKEN is required}"

python /app/backup.py || echo "[entrypoint] WARNING: initial mirror failed; retrying on schedule"
echo "${GITHUB_BACKUP_SCHEDULE:-@daily} python /app/backup.py" > "$CRONTAB_FILE"
exec supercronic "$CRONTAB_FILE"
