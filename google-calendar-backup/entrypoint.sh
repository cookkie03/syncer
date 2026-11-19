#!/bin/bash
set -euo pipefail

LOG_DIR="${LOG_DIR:-/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/google-calendar-backup.log}"
mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

exec python3 /app/backup.py --watch
