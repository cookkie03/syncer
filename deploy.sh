#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is required" >&2
  exit 1
fi
docker info >/dev/null

if [[ ! -f .env ]]; then
  echo "ERROR: .env is missing; copy settings/.env.example and fill credentials" >&2
  exit 1
fi

if ! PYTHONPATH=setup:vdirsyncer:settings python3 setup/check-portable.py; then
  echo "ERROR: deployment preflight failed; no containers were started" >&2
  exit 1
fi

docker compose config --quiet
docker compose up -d --build --remove-orphans

deadline=$((SECONDS + ${DEPLOY_HEALTH_TIMEOUT_SECONDS:-900}))
while (( SECONDS < deadline )); do
  if docker compose ps --status running --services | sort | cmp -s - <(docker compose config --services | sort); then
    if docker compose ps --format json | grep -q '"Health":"unhealthy"'; then
      echo "ERROR: a service became unhealthy" >&2
      docker compose ps
      exit 1
    fi
    if docker compose ps --format json | grep -q '"Health":"healthy"'; then
      if bash check-health.sh; then
        echo "Deployment healthy"
        exit 0
      fi
    fi
  fi
  sleep 5
done

echo "ERROR: deployment did not become healthy within the timeout" >&2
docker compose ps
exit 1
