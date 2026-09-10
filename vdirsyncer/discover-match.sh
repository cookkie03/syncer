#!/bin/sh
set -eu

LOCK_DIR=/tmp/vdirsyncer-discover-match.lock
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[discover-match] another discover is already running"
  exit 0
fi
trap 'rmdir "$LOCK_DIR"' EXIT

echo "[discover-match] Refreshing CalDAV/Google calendar pairing"
python3 /app/refresh_pairing.py --write
python3 /app/render_config.py
yes | vdirsyncer discover caldav_gcal
echo "[discover-match] Pairing refresh complete"