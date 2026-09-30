#!/bin/bash
set -euo pipefail

XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/data}"
export XDG_CONFIG_HOME
CONFIG_DIR="$XDG_CONFIG_HOME/vdirsyncer"
CONFIG_FILE="$CONFIG_DIR/config"
CRONTAB_FILE="/tmp/vdirsyncer.cron"
LOG_DIR="${LOG_DIR:-/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/vdirsyncer.log}"
RUN_STARTUP_SYNC="${VDIRSYNCER_RUN_STARTUP_SYNC:-0}"
RUN_SCHEDULE="${VDIRSYNCER_RUN_SCHEDULE:-1}"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1
# /tmp survives a process restart in the same container. No sync jobs are
# running yet, so clear locks left by an abrupt previous stop.
rmdir /tmp/vdirsyncer-sync.lock 2>/dev/null || true

# ── Validate required environment variables ────────────────────────────────
: "${CALDAV_URL:?CALDAV_URL is required}"
: "${CALDAV_USERNAME:?CALDAV_USERNAME is required}"
: "${CALDAV_PASSWORD:?CALDAV_PASSWORD is required}"
: "${GOOGLE_TOKEN_FILE:?GOOGLE_TOKEN_FILE is required}"
: "${CALENDAR_MAP_FILE:=/app/project-settings/calendar-pairings.json}"

# Default sync interval: 60 minutes
SYNC_INTERVAL_MINUTES="${SYNC_INTERVAL_MINUTES:-60}"

# ── Render config from template ────────────────────────────────────────────
mkdir -p "$CONFIG_DIR"
python3 /app/render_config.py
echo "[entrypoint] Config ready at $CONFIG_FILE"

# Force direct DNS if Docker's internal resolver (127.0.0.11) is broken
if ! python3 -c "import socket; socket.setdefaulttimeout(3); socket.getaddrinfo('google.com', 443)" >/dev/null 2>&1; then
    echo "[entrypoint] DNS broken via Docker resolver — switching to direct 1.1.1.1 + 8.8.8.8"
    printf "nameserver 1.1.1.1\nnameserver 8.8.8.8\n" > /etc/resolv.conf
fi

# ── The sync wrapper performs discovery and pairing before any write ──────
if [ "$RUN_STARTUP_SYNC" = "1" ]; then
  if [ -s "$GOOGLE_TOKEN_FILE" ]; then
    echo "[entrypoint] Running initial vdirsyncer sync..."
    /app/sync-notify.sh || echo "[entrypoint] WARNING: initial sync failed — will retry on schedule"
  else
    echo "[entrypoint] Startup sync skipped: missing $GOOGLE_TOKEN_FILE"
  fi
else
  echo "[entrypoint] Startup sync disabled (VDIRSYNCER_RUN_STARTUP_SYNC=$RUN_STARTUP_SYNC)"
fi

# ── Build crontab and hand off to supercronic ─────────────────────────────
# Accept only exact cron intervals; do not silently shorten an interval.
if [ "$RUN_SCHEDULE" != "1" ]; then
  echo "[entrypoint] Scheduled sync disabled (VDIRSYNCER_RUN_SCHEDULE=$RUN_SCHEDULE)"
  echo "[entrypoint] Container will stay idle until you run /app/sync-notify.sh or enable scheduling"
  exec sh -c 'trap : TERM INT; while true; do sleep 86400; done'
fi

if ! [[ "$SYNC_INTERVAL_MINUTES" =~ ^[0-9]+$ ]] || [ "$SYNC_INTERVAL_MINUTES" -lt 1 ] || [ "$SYNC_INTERVAL_MINUTES" -gt 1440 ]; then
  echo "[entrypoint] SYNC_INTERVAL_MINUTES must be 1..1440" >&2
  exit 1
fi
if [ "$SYNC_INTERVAL_MINUTES" -ge 60 ]; then
  if [ $((SYNC_INTERVAL_MINUTES % 60)) -ne 0 ] || [ $((24 % (SYNC_INTERVAL_MINUTES / 60))) -ne 0 ]; then
    echo "[entrypoint] SYNC_INTERVAL_MINUTES must evenly divide 24 hours" >&2
    exit 1
  fi
  SYNC_HOURS=$(( SYNC_INTERVAL_MINUTES / 60 ))
  CRON_EXPR="0 */${SYNC_HOURS} * * *"
else
  if [ $((60 % SYNC_INTERVAL_MINUTES)) -ne 0 ]; then
    echo "[entrypoint] SYNC_INTERVAL_MINUTES must evenly divide 60 minutes" >&2
    exit 1
  fi
  CRON_EXPR="*/${SYNC_INTERVAL_MINUTES} * * * *"
fi
{
  echo "${CRON_EXPR} if [ -s \"$GOOGLE_TOKEN_FILE\" ]; then /app/sync-notify.sh; else echo '[entrypoint] Scheduled sync skipped: missing Google token'; fi"
} > "$CRONTAB_FILE"
echo "[entrypoint] Scheduling pairing check and sync every ${SYNC_INTERVAL_MINUTES} minute(s) via supercronic"
exec supercronic "$CRONTAB_FILE"
