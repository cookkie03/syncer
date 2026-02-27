#!/bin/sh
set -e

CRONTAB_FILE="/tmp/vtodo-notion.cron"

# Create state directory if it doesn't exist
mkdir -p /data

# Validate required environment variables
: "${CALDAV_URL:?CALDAV_URL is required}"
: "${CALDAV_USERNAME:?CALDAV_USERNAME is required}"
: "${CALDAV_PASSWORD:?CALDAV_PASSWORD is required}"
: "${NOTION_TOKEN:?NOTION_TOKEN is required}"
: "${NOTION_DATABASE_ID:?NOTION_DATABASE_ID is required}"

# Default sync interval: 10 minutes
SYNC_INTERVAL_MINUTES="${SYNC_INTERVAL_MINUTES:-10}"

echo "[entrypoint] Running initial sync..."
python /app/sync.py || echo "[entrypoint] WARNING: initial sync failed — will retry on schedule"

echo "*/${SYNC_INTERVAL_MINUTES} * * * * python /app/sync.py 2>&1" > "$CRONTAB_FILE"
echo "[entrypoint] Scheduling sync every ${SYNC_INTERVAL_MINUTES} minute(s) via supercronic"
exec supercronic "$CRONTAB_FILE"
