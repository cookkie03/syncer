#!/bin/sh
set -eu

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <calendar-name> [<calendar-name> ...]"
  echo "Example: $0 \"Amore\""
  exit 1
fi

STATUS_DIR="/data/status"
BACKUP_DIR="/data/status-bootstrap-backup-$(date +%Y%m%d_%H%M%S)"
CONFIG_FILE="${XDG_CONFIG_HOME:-/data}/vdirsyncer/config"

mkdir -p "$BACKUP_DIR"
if [ -d "$STATUS_DIR" ]; then
  cp -a "$STATUS_DIR"/. "$BACKUP_DIR"/ 2>/dev/null || true
fi
echo "[bootstrap] Backed up status dir to $BACKUP_DIR"

python3 /app/refresh_pairing.py --write
python3 /app/render_config.py

echo "[bootstrap] Discovering current collections..."
vdirsyncer discover caldav_gcal

for CAL_NAME in "$@"; do
  echo "[bootstrap] Syncing collection: $CAL_NAME"
  python3 /app/sync_wrapper.py sync "caldav_gcal/$CAL_NAME"
done

echo "[bootstrap] Completed selected collections"
echo "[bootstrap] Review logs before enabling scheduled sync"
