#!/bin/bash
set -e

# Path to the crontab file
CRONTAB_FILE="/app/crontab"

# Sync interval in minutes (default: 30)
SYNC_INTERVAL=${SYNC_INTERVAL:-30}

echo "Starting carddav-google-contacts sync (interval: ${SYNC_INTERVAL}m)..."

# Create crontab file
# Handle large intervals by converting to hours/days
# Minutes must be 0-59. If SYNC_INTERVAL > 59, we need to use hours/days.
if [ "$SYNC_INTERVAL" -lt 60 ]; then
    CRON_EXPRESSION="*/${SYNC_INTERVAL} * * * *"
elif [ "$((SYNC_INTERVAL % 1440))" -eq 0 ]; then
    DAYS=$((SYNC_INTERVAL / 1440))
    CRON_EXPRESSION="0 0 */${DAYS} * *"
elif [ "$((SYNC_INTERVAL % 60))" -eq 0 ]; then
    HOURS=$((SYNC_INTERVAL / 60))
    if [ "$HOURS" -le 23 ]; then
        CRON_EXPRESSION="0 */${HOURS} * * *"
    else
        # fallback for hours > 23 that aren't multiples of 1440
        CRON_EXPRESSION="0 0 * * *"
    fi
else
    # fallback for non-multiples > 59: run once a day
    echo "Warning: Interval ${SYNC_INTERVAL}m is not a multiple of 60 and > 59. Falling back to daily at 00:00."
    CRON_EXPRESSION="0 0 * * *"
fi

echo "$CRON_EXPRESSION python3 /app/sync.py" > "$CRONTAB_FILE"
echo "Crontab expression: $CRON_EXPRESSION"

# Force direct DNS if Docker's internal resolver (127.0.0.11) is broken
if ! python3 -c "import socket; socket.setdefaulttimeout(3); socket.getaddrinfo('google.com', 443)" >/dev/null 2>&1; then
    echo "[entrypoint] DNS broken via Docker resolver — switching to direct 1.1.1.1 + 8.8.8.8"
    printf "nameserver 1.1.1.1\nnameserver 8.8.8.8\n" > /etc/resolv.conf
fi

# Initial sync
echo "Running initial sync..."
python3 /app/sync.py || echo "Initial sync failed, will retry via cron."

# Start supercronic
exec /usr/local/bin/supercronic "$CRONTAB_FILE"
