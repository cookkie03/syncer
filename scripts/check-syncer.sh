#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

expected="caldav-backup google-contacts-backup spotify-backup vdirsyncer"
actual="$(docker compose config --services | sort | tr '\n' ' ' | sed 's/ $//')"
expected_sorted="$(printf '%s\n' $expected | sort | tr '\n' ' ' | sed 's/ $//')"

if [[ "$actual" != "$expected_sorted" ]]; then
  echo "ERROR: servizi Compose inattesi: $actual" >&2
  exit 1
fi

docker compose ps

for service in $expected; do
  if ! docker compose ps --status running --services | grep -qx "$service"; then
    echo "ERROR: servizio non running: $service" >&2
    exit 1
  fi
done

for path in logs/*/*.log; do
  [[ -f "$path" ]] || continue
  if tail -100 "$path" | grep -Eiq 'Traceback|deleted_client|Session is closed|\[ERROR\]|^ERROR:'; then
    echo "ERROR: problema negli ultimi 100 log: $path" >&2
    exit 1
  fi
done

[[ -f caldav-backup/backup/latest/manifest.json ]] || { echo "ERROR: manifest CalDAV assente" >&2; exit 1; }
[[ -f google-contacts-backup/backup/latest.json ]] || { echo "ERROR: puntatore Contacts assente" >&2; exit 1; }
[[ -f spotify-backup/data/backup/spotify_backup_current.json ]] || { echo "ERROR: backup Spotify assente" >&2; exit 1; }

echo "OK: servizi, log recenti e backup principali verificati"
