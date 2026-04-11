#!/bin/sh
set -e

CRONTAB_FILE="/tmp/spotify-backup.cron"

# Create backup directory if it doesn't exist
mkdir -p /data/backup

# Validate required environment variables
: "${SPOTIFY_CLIENT_ID:?SPOTIFY_CLIENT_ID is required}"
: "${SPOTIFY_CLIENT_SECRET:?SPOTIFY_CLIENT_SECRET is required}"
: "${SPOTIFY_REDIRECT_URI:?SPOTIFY_REDIRECT_URI is required}"

# Default backup interval: 1 day (1440 minutes)
BACKUP_INTERVAL_MINUTES="${BACKUP_INTERVAL_MINUTES:-1440}"

# Handle large intervals by converting to hours/days
if [ "$BACKUP_INTERVAL_MINUTES" -lt 60 ]; then
    CRON_EXPRESSION="*/${BACKUP_INTERVAL_MINUTES} * * * *"
elif [ "$((BACKUP_INTERVAL_MINUTES % 1440))" -eq 0 ]; then
    DAYS=$((BACKUP_INTERVAL_MINUTES / 1440))
    CRON_EXPRESSION="0 ${DAYS:-1} * * *"
elif [ "$((BACKUP_INTERVAL_MINUTES % 60))" -eq 0 ]; then
    HOURS=$((BACKUP_INTERVAL_MINUTES / 60))
    if [ "$HOURS" -le 23 ]; then
        CRON_EXPRESSION="0 */${HOURS} * * *"
    else
        CRON_EXPRESSION="0 0 * * *"
    fi
else
    CRON_EXPRESSION="0 0 * * *"
fi

echo "[entrypoint] Running initial backup..."
python /app/backup.py || echo "[entrypoint] WARNING: initial backup failed — will retry on schedule"

echo "$CRON_EXPRESSION python /app/backup.py 2>&1" > "$CRONTAB_FILE"
echo "[entrypoint] Scheduling backup with expression: $CRON_EXPRESSION via supercronic"
exec supercronic "$CRONTAB_FILE"