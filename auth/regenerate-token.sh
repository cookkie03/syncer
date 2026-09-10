#!/bin/bash
# Script to regenerate Google OAuth token for vdirsyncer
# Run this on the NEW device after deployment

set -e

echo "======================================"
echo "Google OAuth Token Regenerator"
echo "======================================"
echo ""
echo "This script will regenerate Google OAuth tokens"
echo "using an external browser flow suitable for headless hosts."
echo "It authorizes Google Calendar and Google Contacts only."
echo ""

# Check if .env exists (in parent dir or config/)
if [ -f ../.env ]; then
    ENV_FILE="../.env"
elif [ -f .env ]; then
    ENV_FILE=".env"
else
    echo "❌ Error: .env file not found!"
    echo "   Please copy config/.env.example to .env and fill in your credentials."
    exit 1
fi

# Load credentials from .env safely
if [ -f "$ENV_FILE" ]; then
    while IFS='=' read -r key value || [ -n "$key" ]; do
        # Skip comments and empty lines
        case "$key" in
            \#*|"")
                continue
                ;;
        esac
        # Remove leading/trailing whitespace from key and value
        key=$(echo "$key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        value=$(echo "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        # Remove quotes if present
        value=$(echo "$value" | sed 's/^["'"'"']//;s/["'"'"']$//')
        # Export only if key is valid
        if [ -n "$key" ] && [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
            export "$key=$value"
        fi
    done < "$ENV_FILE"
fi

DEVICE_CLIENT_ID="${GOOGLE_DEVICE_CLIENT_ID:-}"
DEVICE_CLIENT_SECRET="${GOOGLE_DEVICE_CLIENT_SECRET:-}"

if [ -z "$DEVICE_CLIENT_ID" ] || [ -z "$DEVICE_CLIENT_SECRET" ]; then
    echo "❌ Error: missing headless Google OAuth credentials in .env"
    echo "   Set GOOGLE_DEVICE_CLIENT_ID and GOOGLE_DEVICE_CLIENT_SECRET."
    exit 1
fi

echo "✓ Found dedicated device-flow credentials in .env"
echo "  Client ID: ${DEVICE_CLIENT_ID:0:20}..."
echo ""

# Create token directory if not exists (in parent dir)
mkdir -p ../vdirsyncer/token

# Backup old token if exists
if [ -f ../vdirsyncer/token/google.json ]; then
    echo "Backing up old token..."
    mv ../vdirsyncer/token/google.json ../vdirsyncer/token/google.json.backup.$(date +%Y%m%d_%H%M%S)
fi

echo ""
echo "======================================"
echo "Starting external-browser OAuth authorization..."
echo "======================================"
echo ""

# Get script and project directories for running authorize-device.py
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Run Python authorization
if command -v python3 &> /dev/null; then
    python3 "$SCRIPT_DIR/authorize-device.py"
elif command -v python &> /dev/null; then
    python "$SCRIPT_DIR/authorize-device.py"
else
    echo "❌ Python not found. Trying with Docker..."
    docker run --rm -it \
        -v "$PROJECT_DIR:/workspace" \
        -w /workspace \
        -e GOOGLE_DEVICE_CLIENT_ID \
        -e GOOGLE_DEVICE_CLIENT_SECRET \
        python:3.11-slim \
        python auth/authorize-device.py
fi

echo ""
echo "======================================"
echo "Token regeneration complete!"
echo "======================================"
echo ""
echo "Next steps:"
echo "1. Recreate the containers that use Google tokens:"
echo "   docker compose up -d --force-recreate vdirsyncer google-contacts-backup"
echo ""
echo "2. Check logs:"
echo "   docker compose logs -f vdirsyncer google-contacts-backup"
