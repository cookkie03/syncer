#!/bin/bash
# Script to regenerate Google OAuth token for vdirsyncer
# Run this on the NEW device after deployment

set -e

echo "======================================"
echo "Google OAuth Token Regenerator"
echo "======================================"
echo ""
echo "This script will regenerate the Google OAuth token"
echo "for the new device (needed after container migration)."
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

if [ -z "$GOOGLE_CLIENT_ID" ] || [ -z "$GOOGLE_CLIENT_SECRET" ]; then
    echo "❌ Error: GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not set in .env"
    exit 1
fi

echo "✓ Found credentials in .env"
echo "  Client ID: ${GOOGLE_CLIENT_ID:0:20}..."
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
echo "Starting OAuth authorization..."
echo "======================================"
echo ""

# Get script directory for running authorize-device.py
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Run Python authorization
if command -v python3 &> /dev/null; then
    python3 "$SCRIPT_DIR/authorize-device.py"
elif command -v python &> /dev/null; then
    python "$SCRIPT_DIR/authorize-device.py"
else
    echo "❌ Python not found. Trying with Docker..."
    docker run --rm -it \
        -v "$SCRIPT_DIR:/workspace" \
        -w /workspace \
        -e GOOGLE_CLIENT_ID \
        -e GOOGLE_CLIENT_SECRET \
        python:3.11-slim \
        python authorize-device.py
fi

echo ""
echo "======================================"
echo "Token regeneration complete!"
echo "======================================"
echo ""
echo "Next steps:"
echo "1. Rebuild the vdirsyncer container:"
echo "   docker-compose down"
echo "   docker-compose build --no-cache vdirsyncer"
echo "   docker-compose up -d vdirsyncer"
echo ""
echo "2. Check logs:"
echo "   docker-compose logs -f vdirsyncer"