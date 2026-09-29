#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Run on a computer with a browser. The tokens are written inside the project,
# so copying the project to another host also copies the active OAuth state.
exec "$PYTHON_BIN" "$PROJECT_DIR/auth/authorize-desktop.py" --force "$@"
