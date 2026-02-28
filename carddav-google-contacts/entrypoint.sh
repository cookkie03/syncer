#!/bin/bash
set -e

# Path to the crontab file
CRONTAB_FILE="/app/crontab"

# Sync interval in minutes (default: 30)
SYNC_INTERVAL=${SYNC_INTERVAL:-30}

echo "Starting carddav-google-contacts sync (interval: ${SYNC_INTERVAL}m)..."

# Create crontab file
# Run sync.py every X minutes
echo "*/${SYNC_INTERVAL} * * * * python3 /app/sync.py" > "$CRONTAB_FILE"

# Initial sync
echo "Running initial sync..."
python3 /app/sync.py || echo "Initial sync failed, will retry via cron."

# Start supercronic
exec /usr/local/bin/supercronic "$CRONTAB_FILE"