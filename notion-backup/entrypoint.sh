#!/bin/sh
set -eu
exec python3 /app/backup.py --watch "$@"
