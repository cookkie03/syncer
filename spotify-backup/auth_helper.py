#!/usr/bin/env python3
"""
Spotify Auth Helper - uses the official Authorization Code with PKCE flow.
No client secret needed.

Two modes:
  1. Local dev (http://localhost) — no SSL, no tunnel needed.
     Set SPOTIFY_REDIRECT_URI=http://localhost:9000/callback and register that URI.
  2. Tunnel mode (https://) — use when Spotify rejects http://localhost.
     Set SPOTIFY_REDIRECT_URI to your public HTTPS tunnel URL
     (e.g. https://abc123.localhost.run/callback from ssh -R 80:localhost:9000 localhost.run).
     Then run: python ./spotify-backup/auth_helper.py
     In another terminal: ssh -R 80:localhost:9000 localhost.run

Run on the HOST machine (not in Docker).
"""

import os
import sys
import base64
import hashlib
import secrets
import ssl
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse
import requests
import json
import time
from pathlib import Path

CLIENT_ID = 'e8c6512e5dc14d47b0e86afa18c86b50'
CACHE_PATH = os.path.expanduser('~/syncer_prod/spotify-backup/data/.cache')

SCOPES = [
    'user-read-private',
    'user-read-email',
    'playlist-read-private',
    'playlist-read-collaborative',
    'user-library-read',
    'user-follow-read',
    'user-top-read',
]

# Defaults
# Spotify requires http://127.0.0.1 (not http://localhost) for local redirect URIs.
# Override via SPOTIFY_REDIRECT_URI env var for tunnel mode (e.g. https://xxx.localhost.run/callback).
DEFAULT_REDIRECT_URI = 'http://127.0.0.1:9000/callback'


def generate_code_verifier(length: int = 128) -> str:
    """Generate a random PKCE code verifier (43-128 chars)."""
    return secrets.token_urlsafe(length)[:length]


def generate_code_challenge(verifier: str) -> str:
    """Generate PKCE code challenge from verifier using SHA256 + base64url."""
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    # base64url encoding without padding
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


class CallbackHandler(BaseHTTPRequestHandler):
    """Handle Spotify OAuth callback on localhost."""

    code_verifier = None
    redirect_uri = None  # set in main() from SPOTIFY_REDIRECT_URI env
    auth_success = False

    def do_GET(self):
        from urllib.parse import parse_qs
        query = urlparse(self.path).query
        params = parse_qs(query)

        code = params.get('code', [None])[0]
        error = params.get('error', [None])[0]

        if error:
            self.send_response(400)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(
                b'<html><body>'
                b'<h1>Authorization Failed</h1>'
                b'<p>You can close this window.</p>'
                b'</body></html>'
            )
            print(f"\nAuthorization error: {error}")
            CallbackHandler.auth_success = False
            return

        if not code:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Error: no code received')
            return

        # Exchange authorization code for access token
        token_url = 'https://accounts.spotify.com/api/token'
        data = {
            'client_id': CLIENT_ID,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': CallbackHandler.redirect_uri,
            'code_verifier': CallbackHandler.code_verifier,
        }

        resp = requests.post(token_url, data=data)
        if resp.status_code != 200:
            print(f"Token request failed: {resp.text}")
            CallbackHandler.auth_success = False
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Error exchanging code for token')
            return

        token_data = resp.json()

        # Save token
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        token_data['expires_at'] = token_data.get('expires_in', 3600) + int(time.time())
        with open(CACHE_PATH, 'w') as f:
            json.dump({
                'access_token': token_data['access_token'],
                'token_type': token_data.get('token_type', 'Bearer'),
                'expires_in': token_data.get('expires_in', 3600),
                'expires_at': token_data.get('expires_at'),
                'refresh_token': token_data.get('refresh_token'),
                'scope': ' '.join(SCOPES),
            }, f, indent=2)

        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(
            b'<html><body>'
            b'<h1>Authentication Successful!</h1>'
            b'<p>You can close this window and return to the terminal.</p>'
            b'</body></html>'
        )
        print(f"\nToken saved to: {CACHE_PATH}")
        CallbackHandler.auth_success = True

    def log_message(self, format, *args):
        """Suppress default request logging."""
        pass


def main():
    # ── Redirect URI from env or default ──────────────────────────────
    redirect_uri = os.environ.get('SPOTIFY_REDIRECT_URI', DEFAULT_REDIRECT_URI)
    parsed = urlparse(redirect_uri)
    is_https = parsed.scheme == 'https'
    is_localhost = parsed.hostname in ('localhost', '127.0.0.1')

    # ── Local port to listen on ──────────────────────────────────────
    # Extract port from redirect_uri, or use SPOTIFY_AUTH_PORT, or default 9000
    listen_port = int(os.environ.get('SPOTIFY_AUTH_PORT', parsed.port or 9000))

    # ── Server setup ─────────────────────────────────────────────────
    server = HTTPServer(('localhost', listen_port), CallbackHandler)
    CallbackHandler.redirect_uri = redirect_uri

    print("=" * 50)
    print("Spotify Auth Helper  (PKCE flow)")
    print("=" * 50)
    print(f"Redirect URI: {redirect_uri}")
    if is_https and not is_localhost:
        print("Mode: tunnel (https://) — keep the tunnel running!")
    elif is_https and is_localhost:
        print("Mode: https on localhost — using local SSL certs")
    else:
        print("Mode: plain http on localhost")
    print()

    # Generate PKCE verifier and challenge
    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)
    CallbackHandler.code_verifier = code_verifier

    # Build authorization URL
    auth_params = {
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'code_challenge_method': 'S256',
        'code_challenge': code_challenge,
        'scope': ' '.join(SCOPES),
    }
    auth_url = 'https://accounts.spotify.com/authorize?' + urlencode(auth_params)

    print(f"1. Opening browser for Spotify login...")
    webbrowser.open(auth_url)

    print(f"2. Waiting for callback on {redirect_uri} ...")
    server.handle_request()
    server.server_close()

    if CallbackHandler.auth_success:
        print("\nAuth complete!")
    else:
        print("\nAuth failed!")
        sys.exit(1)

    print(f"Token location: {CACHE_PATH}")


if __name__ == '__main__':
    main()