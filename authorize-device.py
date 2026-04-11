#!/usr/bin/env python3
"""
Google OAuth authorization - opens browser, user pastes code.
Uses standard OAuth 2.0 flow with manual code entry.
"""
import json
import sys
import subprocess
import pathlib
import re
import webbrowser
import requests
from urllib.parse import urlencode

def load_env(path: pathlib.Path) -> dict:
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def standard_flow(client_id, client_secret, scopes, token_path, service_name):
    """Standard OAuth flow with manual code entry."""
    print(f"\n{'='*60}")
    print(f"Authorization for: {service_name}")
    print('='*60)
    
    # Generate state for security
    state = "state_" + str(hash(service_name))[-8:]
    
    # Build authorization URL
    # IMPORTANT: access_type=offline and prompt=consent are required to get a refresh_token
    # Without these, the token will expire after 1 hour and cannot be refreshed
    params = {
        "client_id": client_id,
        "redirect_uri": "http://localhost:8888",
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    
    print(f"\n1. Opening browser for authorization...")
    print(f"   (URL: {auth_url[:80]}...)")
    
    # Try to open browser
    try:
        webbrowser.open(auth_url)
        print("   Browser opened!")
    except:
        print("   Please manually open the URL in your browser")
    
    print(f"\n2. After approving, you'll be redirected to:")
    print(f"   http://localhost:8888?code=XXXXX&state={state}")
    print(f"\n3. Copy the 'code' parameter value from the URL and paste it here:")
    
    code = input("   Code: ").strip()
    
    if not code:
        print("No code entered, skipping...")
        return False
    
    # Exchange code for tokens
    print("\n4. Exchanging code for tokens...")
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": "http://localhost:8888",
            "grant_type": "authorization_code",
        }
    ).json()
    
    if "access_token" not in resp:
        print(f"ERROR: {resp}")
        return False
    
    # Add token URI info if missing (for google-auth library compatibility)
    if "token_uri" not in resp:
        resp["token_uri"] = "https://oauth2.googleapis.com/token"
    if "client_id" not in resp:
        resp["client_id"] = client_id
    if "client_secret" not in resp:
        resp["client_secret"] = client_secret
    
    with open(token_path, "w") as f:
        json.dump(resp, f, indent=2)
    
    print(f"✓ Token saved to: {token_path}")
    return True


def main():
    root = pathlib.Path(__file__).parent
    env_file = root / ".env"
    
    if not env_file.exists():
        print(f"ERROR: {env_file} not found.")
        sys.exit(1)
    
    env = load_env(env_file)
    
    required = ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        print("ERROR: Missing:", ", ".join(missing))
        sys.exit(1)
    
    token_dir = root / "vdirsyncer" / "token"
    token_dir.mkdir(parents=True, exist_ok=True)
    
    client_id = env["GOOGLE_CLIENT_ID"]
    client_secret = env["GOOGLE_CLIENT_SECRET"]
    
    success_count = 0
    
    # 1. Google Calendar token
    calendar_token = token_dir / "google.json"
    if not calendar_token.exists():
        if standard_flow(client_id, client_secret, 
                       ["https://www.googleapis.com/auth/calendar"],
                       calendar_token, "Google Calendar"):
            success_count += 1
    else:
        print(f"Calendar token already exists: {calendar_token}")
        success_count += 1
    
    # 2. Google Contacts token  
    contacts_token = token_dir / "google_contacts.json"
    if not contacts_token.exists():
        if standard_flow(client_id, client_secret,
                        ["https://www.googleapis.com/auth/contacts"],
                        contacts_token, "Google Contacts"):
            success_count += 1
    else:
        print(f"Contacts token already exists: {contacts_token}")
        success_count += 1
    
    # 3. Gmail token
    gmail_token = token_dir / "google_gmail.json"
    if not gmail_token.exists():
        if standard_flow(client_id, client_secret,
                        ["https://www.googleapis.com/auth/gmail.readonly"],
                        gmail_token, "Gmail"):
            success_count += 1
    else:
        print(f"Gmail token already exists: {gmail_token}")
        success_count += 1
    
    print("\n" + "="*60)
    if success_count == 3:
        print("All authorizations complete!")
    else:
        print(f"Partial success: {success_count}/3 tokens created")
    print("="*60)
    print(f"Tokens in: {token_dir}")
    print("\nNow restart services:")
    print("  docker-compose restart")


if __name__ == "__main__":
    main()