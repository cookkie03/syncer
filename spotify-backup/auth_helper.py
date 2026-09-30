#!/usr/bin/env python3
"""Authorize Spotify with PKCE on the PC and save the portable token cache.

The redirect URI comes from the project .env. Local HTTPS needs cert.pem/key.pem;
a remote HTTPS redirect needs an independently configured tunnel to this PC.
"""

import os
import sys
import base64
import hashlib
import secrets
import ssl
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'setup'))
from project_env import load_project_env

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / 'data'


def project_setting(name: str, default: str = '') -> str:
    """Use host environment first, then the portable project .env file."""
    if name in os.environ:
        return os.environ[name]
    env_file = PROJECT_DIR.parent / '.env'
    if env_file.exists():
        return load_project_env(env_file).get(name, default)
    return default


CLIENT_ID = project_setting('SPOTIFY_CLIENT_ID')

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


def default_cache_path() -> Path:
    """Return the portable cache path stored inside the repo."""
    return DATA_DIR / '.cache'


def resolved_cache_path() -> Path:
    """Allow overrides while defaulting to the repo-local cache path."""
    return Path(os.environ.get('SPOTIFY_CACHE_PATH', default_cache_path()))


def should_open_browser() -> bool:
    """Open a local browser only when explicitly requested."""
    return os.environ.get('SPOTIFY_AUTH_OPEN_BROWSER', '').lower() in {'1', 'true', 'yes'}


def normalize_callback_url(callback_url: str) -> str:
    """Accept copied loopback URLs with or without scheme."""
    callback_url = callback_url.strip()
    if callback_url.startswith(('http://', 'https://')):
        return callback_url
    if callback_url.startswith(('127.0.0.1', 'localhost')):
        return f'http://{callback_url}'
    return callback_url


def extract_callback_params(callback_url: str) -> dict[str, str | None]:
    """Extract auth response parameters from a pasted callback URL."""
    parsed = urlparse(normalize_callback_url(callback_url))
    params = parse_qs(parsed.query)
    return {
        'code': params.get('code', [None])[0],
        'error': params.get('error', [None])[0],
        'state': params.get('state', [None])[0],
    }


def write_token_cache(token_data: dict) -> Path:
    """Persist the OAuth token cache inside the repo."""
    cache_path = resolved_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    token_data['expires_at'] = token_data.get('expires_in', 3600) + int(time.time())
    temporary = cache_path.with_name(cache_path.name + '.tmp')
    with open(temporary, 'w', encoding='utf-8') as f:
        json.dump({
            'access_token': token_data['access_token'],
            'token_type': token_data.get('token_type', 'Bearer'),
            'expires_in': token_data.get('expires_in', 3600),
            'expires_at': token_data.get('expires_at'),
            'refresh_token': token_data.get('refresh_token'),
            'scope': ' '.join(SCOPES),
        }, f, indent=2)
    temporary.chmod(0o600)
    temporary.replace(cache_path)
    return cache_path


def exchange_code_for_token(code: str, code_verifier: str, redirect_uri: str) -> Path:
    """Exchange an authorization code and persist the resulting token."""
    token_url = 'https://accounts.spotify.com/api/token'
    data = {
        'client_id': CLIENT_ID,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
        'code_verifier': code_verifier,
    }

    request = Request(
        token_url,
        data=urlencode(data).encode('utf-8'),
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Spotify token request failed with HTTP {exc.code}") from exc

    return write_token_cache(payload)


def handle_callback_result(code: str | None, error: str | None, code_verifier: str, redirect_uri: str) -> bool:
    """Handle a callback result from either HTTP listener or pasted URL."""
    if error:
        print(f"\nAuthorization error: {error}")
        return False

    if not code:
        print("\nAuthorization error: no code received")
        return False

    try:
        cache_path = exchange_code_for_token(code, code_verifier, redirect_uri)
    except Exception as exc:
        print(exc)
        return False

    print(f"\nToken saved to: {cache_path}")
    return True


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
    expected_state = None
    redirect_uri = None  # set in main() from SPOTIFY_REDIRECT_URI env
    auth_success = False

    def do_GET(self):
        params = extract_callback_params(self.path)
        if params['state'] != CallbackHandler.expected_state:
            self.send_error(400, 'OAuth state mismatch')
            return
        success = handle_callback_result(
            params['code'],
            params['error'],
            CallbackHandler.code_verifier,
            CallbackHandler.redirect_uri,
        )

        if not success:
            self.send_response(400)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(
                b'<html><body>'
                b'<h1>Authorization Failed</h1>'
                b'<p>You can close this window.</p>'
                b'</body></html>'
            )
            CallbackHandler.auth_success = False
            return

        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(
            b'<html><body>'
            b'<h1>Authentication Successful!</h1>'
            b'<p>You can close this window and return to the terminal.</p>'
            b'</body></html>'
        )
        CallbackHandler.auth_success = True

    def log_message(self, format, *args):
        """Suppress default request logging."""
        pass


def main():
    # ── Redirect URI from env or default ──────────────────────────────
    if not CLIENT_ID:
        raise RuntimeError('SPOTIFY_CLIENT_ID is missing from .env or the host environment')
    redirect_uri = project_setting('SPOTIFY_REDIRECT_URI', DEFAULT_REDIRECT_URI)
    parsed = urlparse(redirect_uri)
    is_https = parsed.scheme == 'https'
    is_localhost = parsed.hostname in ('localhost', '127.0.0.1')

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
    CallbackHandler.expected_state = secrets.token_urlsafe(24)
    CallbackHandler.auth_success = False

    # Build authorization URL
    auth_params = {
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'code_challenge_method': 'S256',
        'code_challenge': code_challenge,
        'state': CallbackHandler.expected_state,
        'scope': ' '.join(SCOPES),
    }
    auth_url = 'https://accounts.spotify.com/authorize?' + urlencode(auth_params)

    listen_port = int(os.environ.get('SPOTIFY_AUTH_PORT', parsed.port or 9000))
    server = HTTPServer(('localhost', listen_port), CallbackHandler)
    if is_https and is_localhost:
        cert = PROJECT_DIR / 'cert.pem'
        key = PROJECT_DIR / 'key.pem'
        if not cert.is_file() or not key.is_file():
            server.server_close()
            raise RuntimeError('HTTPS localhost requires spotify-backup/cert.pem and key.pem')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert), keyfile=str(key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    CallbackHandler.redirect_uri = redirect_uri

    print("Open this URL in a browser on the PC:")
    print(auth_url)
    print(f"Waiting for callback on {redirect_uri} ...")
    if should_open_browser():
        webbrowser.open(auth_url)
    try:
        server.handle_request()
    finally:
        server.server_close()

    if CallbackHandler.auth_success:
        print("\nAuth complete!")
    else:
        print("\nAuth failed!")
        sys.exit(1)

    print(f"Token location: {resolved_cache_path()}")


if __name__ == '__main__':
    main()
