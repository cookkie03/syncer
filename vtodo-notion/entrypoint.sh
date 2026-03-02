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

# Handle large intervals by converting to hours/days
# Minutes must be 0-59. If SYNC_INTERVAL_MINUTES > 59, we need to use hours/days.
if [ "$SYNC_INTERVAL_MINUTES" -lt 60 ]; then
    CRON_EXPRESSION="*/${SYNC_INTERVAL_MINUTES} * * * *"
elif [ "$((SYNC_INTERVAL_MINUTES % 1440))" -eq 0 ]; then
    DAYS=$((SYNC_INTERVAL_MINUTES / 1440))
    CRON_EXPRESSION="0 0 */${DAYS} * *"
elif [ "$((SYNC_INTERVAL_MINUTES % 60))" -eq 0 ]; then
    HOURS=$((SYNC_INTERVAL_MINUTES / 60))
    if [ "$HOURS" -le 23 ]; then
        CRON_EXPRESSION="0 */${HOURS} * * *"
    else
        # fallback for hours > 23 that aren't multiples of 1440
        CRON_EXPRESSION="0 0 * * *"
    fi
else
    # fallback for non-multiples > 59: run once a day
    echo "Warning: Interval ${SYNC_INTERVAL_MINUTES}m is not a multiple of 60 and > 59. Falling back to daily at 00:00."
    CRON_EXPRESSION="0 0 * * *"
fi

echo "[entrypoint] Running initial sync..."
python /app/sync.py || echo "[entrypoint] WARNING: initial sync failed — will retry on schedule"

echo "$CRON_EXPRESSION python /app/sync.py 2>&1" > "$CRONTAB_FILE"
echo "[entrypoint] Scheduling sync with expression: $CRON_EXPRESSION via supercronic"
exec supercronic "$CRONTAB_FILE"
