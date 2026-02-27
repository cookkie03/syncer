#!/bin/sh
set -e

CONFIG_DIR="${XDG_CONFIG_HOME:-/root/.config}/vdirsyncer"
CONFIG_FILE="$CONFIG_DIR/config"
CRONTAB_FILE="/tmp/vdirsyncer.cron"

# ── Validate required environment variables ────────────────────────────────
: "${CALDAV_URL:?CALDAV_URL is required}"
: "${CALDAV_USERNAME:?CALDAV_USERNAME is required}"
: "${CALDAV_PASSWORD:?CALDAV_PASSWORD is required}"
: "${CARDDAV_URL:?CARDDAV_URL is required}"
: "${GOOGLE_TOKEN_FILE:?GOOGLE_TOKEN_FILE is required}"
: "${GOOGLE_CLIENT_ID:?GOOGLE_CLIENT_ID is required}"
: "${GOOGLE_CLIENT_SECRET:?GOOGLE_CLIENT_SECRET is required}"

# Default sync interval: 60 minutes
SYNC_INTERVAL_MINUTES="${SYNC_INTERVAL_MINUTES:-60}"

# ── Render config from template ────────────────────────────────────────────
mkdir -p "$CONFIG_DIR"
envsubst < /app/config.template > "$CONFIG_FILE"
echo "[entrypoint] Config written to $CONFIG_FILE"

# ── Run initial discover + sync ────────────────────────────────────────────
echo "[entrypoint] Running initial vdirsyncer discover..."
yes | vdirsyncer discover || {
  echo "[entrypoint] WARNING: discover failed — token may need refresh or server unreachable"
}

echo "[entrypoint] Running initial vdirsyncer sync..."
/app/sync-notify.sh || echo "[entrypoint] WARNING: initial sync failed — will retry on schedule"

# ── Build crontab and hand off to supercronic ─────────────────────────────
# */N is only valid when N <= max field value (59 for minutes).
# For intervals >= 60 min, convert to hours: every N hours at minute 0.
if [ "$SYNC_INTERVAL_MINUTES" -ge 60 ]; then
  SYNC_HOURS=$(( SYNC_INTERVAL_MINUTES / 60 ))
  CRON_EXPR="0 */${SYNC_HOURS} * * *"
else
  CRON_EXPR="*/${SYNC_INTERVAL_MINUTES} * * * *"
fi
echo "${CRON_EXPR} /app/sync-notify.sh 2>&1" > "$CRONTAB_FILE"
echo "[entrypoint] Scheduling sync every ${SYNC_INTERVAL_MINUTES} minute(s) via supercronic"
exec supercronic "$CRONTAB_FILE"
