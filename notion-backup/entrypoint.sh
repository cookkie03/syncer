#!/bin/sh
set -e

CRONTAB_FILE="/tmp/notion-backup.cron"

: "${NOTION_TOKEN:?NOTION_TOKEN is required}"

# Default: daily at 02:00 UTC. Override with BACKUP_SCHEDULE (cron syntax).
# Default: daily at 03:00 UTC for export download.
BACKUP_SCHEDULE="${BACKUP_SCHEDULE:-0 2 * * *}"
EXPORT_SCHEDULE="${EXPORT_SCHEDULE:-0 3 * * *}"

# Force direct DNS if Docker's internal resolver (127.0.0.11) is broken
if ! python3 -c "import socket; socket.setdefaulttimeout(3); socket.getaddrinfo('api.notion.com', 443)" >/dev/null 2>&1; then
    echo "[entrypoint] DNS broken via Docker resolver — switching to direct 1.1.1.1 + 8.8.8.8"
    printf "nameserver 1.1.1.1\nnameserver 8.8.8.8\n" > /etc/resolv.conf
fi

echo "[entrypoint] Starting Telegram bot in background..."
python3 /app/telegram_bot.py &

echo "[entrypoint] Running initial backup..."
python3 /app/backup.py || echo "[entrypoint] WARNING: initial backup failed — will retry on schedule"

echo "[entrypoint] Running initial export check..."
python3 /app/download_export.py || echo "[entrypoint] WARNING: initial export check failed — will retry on schedule"

echo "${BACKUP_SCHEDULE} python3 /app/backup.py 2>&1" > "$CRONTAB_FILE"
echo "${EXPORT_SCHEDULE} python3 /app/download_export.py 2>&1" >> "$CRONTAB_FILE"
echo "[entrypoint] Scheduling backup '${BACKUP_SCHEDULE}' and export check '${EXPORT_SCHEDULE}' via supercronic"
exec supercronic "$CRONTAB_FILE"
