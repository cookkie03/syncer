#!/usr/bin/env python3
"""
Token refresh utility for vdirsyncer.
Automatically refreshes Google OAuth tokens before they expire.
This makes tokens portable across devices.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests


def refresh_google_token(token_path: Path, client_id: str, client_secret: str) -> bool:
    """
    Refresh a Google OAuth token using the refresh_token.
    Returns True if token was refreshed or is still valid.
    """
    if not token_path.exists():
        print(f"[token_refresh] Token file not found: {token_path}")
        return False
    
    try:
        with open(token_path, "r") as f:
            token_data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[token_refresh] Error reading token file: {e}")
        return False
    
    # Check if we have a refresh token
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        print(f"[token_refresh] No refresh_token found in {token_path}")
        print(f"[token_refresh] Token may need to be regenerated with 'access_type=offline'")
        return False
    
    # Check if token needs refresh (expires in less than 5 minutes or expired)
    expires_in = token_data.get("expires_in", 3600)
    # Use created_at or expiry time if available
    expiry = token_data.get("expiry")
    if expiry:
        # Convert RFC3339 to timestamp
        from datetime import datetime
        try:
            expiry_time = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
            needs_refresh = expiry_time.timestamp() - time.time() < 300  # 5 min buffer
        except:
            needs_refresh = True
    else:
        # No expiry info, assume we need refresh to be safe
        needs_refresh = True
    
    if not needs_refresh:
        print(f"[token_refresh] Token {token_path.name} is still valid")
        return True
    
    # Perform token refresh
    print(f"[token_refresh] Refreshing token: {token_path.name}")
    
    try:
        response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30
        )
        response.raise_for_status()
        new_token = response.json()
        
        # Preserve the refresh_token (Google doesn't always return it)
        new_token["refresh_token"] = refresh_token
        
        # Add client info if missing (for google-auth compatibility)
        if "client_id" not in new_token:
            new_token["client_id"] = client_id
        if "client_secret" not in new_token:
            new_token["client_secret"] = client_secret
        if "token_uri" not in new_token:
            new_token["token_uri"] = "https://oauth2.googleapis.com/token"
        
        # Write updated token
        with open(token_path, "w") as f:
            json.dump(new_token, f, indent=2)
        
        print(f"[token_refresh] Successfully refreshed token: {token_path.name}")
        return True
        
    except requests.RequestException as e:
        print(f"[token_refresh] Failed to refresh token: {e}")
        return False
    except Exception as e:
        print(f"[token_refresh] Unexpected error: {e}")
        return False


def main():
    """Refresh all Google tokens."""
    token_dir = Path("/data/token")
    
    if not token_dir.exists():
        print(f"[token_refresh] Token directory not found: {token_dir}")
        sys.exit(1)
    
    # Get credentials from environment
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        print("[token_refresh] GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not set")
        sys.exit(1)
    
    # Tokens to refresh
    token_files = [
        "google.json",           # Calendar
        "google_contacts.json",  # Contacts
        "google_gmail.json",     # Gmail
    ]
    
    success = True
    for token_file in token_files:
        token_path = token_dir / token_file
        if token_path.exists():
            if not refresh_google_token(token_path, client_id, client_secret):
                success = False
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()