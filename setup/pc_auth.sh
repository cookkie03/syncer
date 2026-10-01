#!/usr/bin/env bash
# Prepare portable OAuth state on a computer with a browser.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [ ! -f .env ]; then
  cp settings/.env.example .env
  echo "Created .env. Fill the active CalDAV, Spotify and Todoist settings, then rerun this command." >&2
  exit 1
fi

python3 setup/migrate-storage.py --apply
python3 setup/check-portable.py --config-only

INCLUDE_GMAIL=0
for argument in "$@"; do
  if [ "$argument" = "--include-gmail" ]; then
    INCLUDE_GMAIL=1
  fi
done

AUTH_VENV="${SYNCER_AUTH_VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/syncer-auth-venv}"
if [ ! -x "$AUTH_VENV/bin/python" ]; then
  python3 -m venv "$AUTH_VENV"
fi
if ! "$AUTH_VENV/bin/python" -c 'import google_auth_oauthlib' >/dev/null 2>&1; then
  "$AUTH_VENV/bin/python" -m pip install google-auth-oauthlib
fi

"$AUTH_VENV/bin/python" setup/google/authorize-desktop.py "$@"

if ! python3 setup/check-portable.py --spotify-ready; then
  echo "Spotify authorization is needed. The browser will open on this PC."
  SPOTIFY_AUTH_OPEN_BROWSER=1 python3 spotify-backup/auth_helper.py
fi

if [ "$INCLUDE_GMAIL" = "1" ]; then
  python3 setup/check-portable.py --include-gmail
else
  python3 setup/check-portable.py
fi
printf '\nReady to copy this entire folder to the NAS. There run: docker compose up -d --build\n'
