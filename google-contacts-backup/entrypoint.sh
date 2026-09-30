#!/bin/bash
set -euo pipefail

LOG_DIR="${LOG_DIR:-/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/google-contacts-backup.log}"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "Starting Google Contacts incremental backup..."

# Force direct DNS if Docker's internal resolver (127.0.0.11) is broken
if ! python3 -c "import socket; socket.setdefaulttimeout(3); socket.getaddrinfo('google.com', 443)" >/dev/null 2>&1; then
    echo "[entrypoint] DNS broken via Docker resolver — switching to direct 1.1.1.1 + 8.8.8.8"
    printf "nameserver 1.1.1.1\nnameserver 8.8.8.8\n" > /etc/resolv.conf
fi

exec python3 /app/backup.py --watch
