#!/bin/bash
set -euo pipefail

CRONTAB_FILE="/tmp/spotify-backup.cron"
LOG_DIR="${LOG_DIR:-/logs}"
BACKUP_DIR="${BACKUP_DIR:-/data/backup}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/spotify-backup.log}"

mkdir -p "$LOG_DIR" "$BACKUP_DIR"
touch "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

# Validate required environment variables
: "${SPOTIFY_CLIENT_ID:?SPOTIFY_CLIENT_ID is required}"
: "${SPOTIFY_CLIENT_SECRET:?SPOTIFY_CLIENT_SECRET is required}"
: "${SPOTIFY_REDIRECT_URI:?SPOTIFY_REDIRECT_URI is required}"

BACKUP_SCHEDULE="${SPOTIFY_BACKUP_SCHEDULE:-0 */4 * * *}"

echo "[entrypoint] Running initial backup..."
python /app/backup.py || echo "[entrypoint] WARNING: initial backup failed — will retry on schedule"

echo "$BACKUP_SCHEDULE python /app/backup.py" > "$CRONTAB_FILE"
echo "[entrypoint] Scheduling backup with expression: $BACKUP_SCHEDULE via supercronic"
exec supercronic "$CRONTAB_FILE"
