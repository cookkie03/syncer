#!/usr/bin/env python3
"""
Google OAuth authorization - uses local HTTP server to catch redirect.
Run from the project root: python authorize-local.py
"""
import json
import sys
import http.server
import threading
import pathlib
import webbrowser
import requests
from urllib.parse import urlencode, parse_qs
from urllib.parse import urlparse

# Global to store the authorization code
auth_code = None
server_ready = threading.Event()

def load_env(path: pathlib.Path) -> dict:
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


class AuthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        
        if 'code' in params:
            auth_code = params['code'][0]
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<html><body><h1>Authorization successful!</h1><p>You can close this window and return to the terminal.</p></body></html>')
            server_ready.set()
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Missing code parameter')
    
    def log_message(self, format, *args):
        pass  # Suppress logging


def run_server(port):
    server = http.server.HTTPServer(('localhost', port), AuthHandler)
    server.handle_request()  # Handle one request
    server.server_close()


def standard_flow(client_id, client_secret, scopes, token_path, service_name):
    """Standard OAuth flow with local server."""
    global auth_code, server_ready
    
    print(f"\n{'='*60}")
    print(f"Authorization for: {service_name}")
    print('='*60)
    
    port = 8888
    
    # Start local server in background
    server_thread = threading.Thread(target=run_server, args=(port,), daemon=True)
    server_thread.start()
    
    # Build authorization URL with localhost redirect
    params = {
        "client_id": client_id,
        "redirect_uri": f"http://localhost:{port}",
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    
    print(f"\n1. Opening browser for authorization...")
    print(f"   URL: {auth_url[:100]}...")
    
    try:
        webbrowser.open(auth_url)
        print("   Browser opened!")
    except:
        print("   Please manually open the URL in your browser")
    
    print(f"\n2. In the browser:")
    print(f"   - Login with your Google account")
    print(f"   - Allow the requested permissions")
    print(f"   - Wait for this page to show 'Authorization successful!'")
    print(f"\n   Waiting for authorization...")
    
    # Wait for server to receive the code
    server_ready.wait(timeout=120)
    
    if not auth_code:
        print("Timeout waiting for authorization. Please try again.")
        return False
    
    print(f"\n3. Received authorization code!")
    
    # Exchange code for tokens
    print("4. Exchanging code for tokens...")
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": auth_code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": f"http://localhost:{port}",
            "grant_type": "authorization_code",
        }
    ).json()
    
    if "access_token" not in resp:
        print(f"ERROR: {resp}")
        return False
    
    # Add metadata for google-auth library compatibility
    resp["token_uri"] = "https://oauth2.googleapis.com/token"
    resp["client_id"] = client_id
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