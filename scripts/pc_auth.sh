#!/usr/bin/env bash
# pc_auth.sh — Google OAuth token generation for Syncer
# Run on PC (not on NAS) before copying project to NAS.
#
# What it does:
#   1. Check Python 3 + pip availability
#   2. Create .venv/ virtual environment if not present
#   3. pip install -r requirements.txt
#   4. Run authorize-google.py for Calendar, Contacts, and Gmail
#   5. Verify tokens are created in vdirsyncer/token/
#   6. Print next steps

set -e

# ── Colours ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Colour

# ── Project root (script directory) ─────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# ── Helpers ──────────────────────────────────────────────────────────────────
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

need()  { command -v "$1" >/dev/null 2>&1 || err "$1 is required but not found. Please install $1."; }

# ── 1. Check prerequisites ────────────────────────────────────────────────────
info "Checking prerequisites..."

need python3
need pip

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Python version: $PYTHON_VERSION"

# ── 2. Virtual environment ───────────────────────────────────────────────────
VENV_DIR="$PROJECT_ROOT/.venv"

if [ ! -d "$VENV_DIR" ]; then
    info "Creating virtual environment in .venv/..."
    python3 -m venv "$VENV_DIR"
else
    info "Virtual environment already exists in .venv/"
fi

# ── 3. Install requirements ─────────────────────────────────────────────────
info "Installing Python dependencies (this may take a minute)..."

# Activate venv pip
if [ -f "$VENV_DIR/bin/pip" ]; then
    PIP="$VENV_DIR/bin/pip"
else
    PIP="pip"
fi

"$PIP" install --upgrade pip
"$PIP" install -r "$PROJECT_ROOT/requirements.txt"

# ── 4. Run authorization ─────────────────────────────────────────────────────
info "Running Google authorization..."
info "A browser window will open for each service. Log in and click Allow."
echo ""

python3 "$PROJECT_ROOT/authorize-google.py"

# ── 5. Verify tokens ─────────────────────────────────────────────────────────
TOKEN_DIR="$PROJECT_ROOT/vdirsyncer/token"
REQUIRED_TOKENS=(
    "$TOKEN_DIR/google.json"
    "$TOKEN_DIR/google_contacts.json"
    "$TOKEN_DIR/google_gmail.json"
)

info "Verifying tokens..."
all_ok=true
for token_file in "${REQUIRED_TOKENS[@]}"; do
    if [ -f "$token_file" ]; then
        info "  $(basename "$token_file") — OK"
    else
        warn "  $(basename "$token_file") — MISSING"
        all_ok=false
    fi
done

if [ "$all_ok" = false ]; then
    err "Some tokens are missing. See errors above."
fi

# ── 6. Done ──────────────────────────────────────────────────────────────────
echo ""
info "═══════════════════════════════════════════════════════════════════════════"
info "  All tokens generated successfully!"
info "═══════════════════════════════════════════════════════════════════════════"
echo ""
info "Next steps:"
echo ""
echo "  1. Copy the project folder to your NAS:"
echo "       rsync -av --exclude='.git/' $PROJECT_ROOT/ user@nas:/volume1/docker/syncer/"
echo ""
echo "  2. On your NAS, build and start services:"
echo "       docker compose up -d --build"
echo ""
echo "  3. Follow logs:"
echo "       docker compose logs -f"
echo ""
info "See AUTH_PC.md for full documentation."
