#!/usr/bin/env bash
# Check running containers and recent successful outputs on the NAS.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

expected="caldav-backup google-contacts-backup spotify-backup vdirsyncer"
actual="$(docker compose config --services | sort | tr '\n' ' ' | sed 's/ $//')"
expected_sorted="$(printf '%s\n' $expected | sort | tr '\n' ' ' | sed 's/ $//')"
if [[ "$actual" != "$expected_sorted" ]]; then
  echo "ERROR: servizi Compose inattesi: $actual" >&2
  exit 1
fi

docker compose ps
for service in $expected; do
  container="$(docker compose ps -q "$service")"
  if [[ -z "$container" ]]; then
    echo "ERROR: container assente: $service" >&2
    exit 1
  fi
  health="$(docker inspect --format '{{.State.Health.Status}}' "$container")"
  if [[ "$health" != healthy ]]; then
    echo "ERROR: $service non healthy ($health)" >&2
    exit 1
  fi
done

python3 - <<'PY'
import json
from pathlib import Path
from time import time

outputs = {
    'CalDAV': ('caldav-backup/backup/latest/manifest.json', 12 * 3600),
    'Google Contacts': ('google-contacts-backup/backup/latest.json', 48 * 3600),
    'Spotify': ('spotify-backup/data/backup/spotify_backup_current.json', 12 * 3600),
    'vdirsyncer': ('vdirsyncer/status/last-success', 4 * 3600),
}
failed = False
for service, (name, max_age) in outputs.items():
    path = Path(name)
    if not path.is_file() or not path.stat().st_size:
        print(f'ERROR: {service}: risultato assente: {name}')
        failed = True
        continue
    age = time() - path.stat().st_mtime
    if age > max_age:
        print(f'ERROR: {service}: ultimo risultato troppo vecchio ({age / 3600:.1f} ore)')
        failed = True
    else:
        print(f'OK: {service}: ultimo risultato {age / 3600:.1f} ore fa')

spotify_current = Path('spotify-backup/data/backup/spotify_backup_current.json')
if spotify_current.is_file():
    try:
        playlists = json.loads(spotify_current.read_text(encoding='utf-8')).get('playlists', [])
        incomplete = sum(bool(playlist.get('tracks_error')) for playlist in playlists)
        if incomplete:
            print(f'WARN: Spotify: brani non accessibili per {incomplete} playlist nel backup corrente')
    except (OSError, ValueError, TypeError, AttributeError):
        print('ERROR: Spotify: backup corrente non leggibile')
        failed = True
if failed:
    raise SystemExit(1)
PY
