#!/usr/bin/env python3
"""
Google OAuth device flow for headless environments.

This flow is intended for authorization from an external browser on another
device. The local machine only prints the verification URL and polls for the
token.
"""

from __future__ import annotations

import json
import pathlib
import ssl
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEVICE_GRANT_TYPE = "http://oauth.net/grant_type/device/1.0"
CA_BUNDLE_CANDIDATES = (
    pathlib.Path("/etc/ssl/certs/ca-certificates.crt"),
    pathlib.Path("/etc/pki/tls/certs/ca-bundle.crt"),
    pathlib.Path("/etc/ssl/cert.pem"),
)


def load_env(path: pathlib.Path) -> dict[str, str]:
    env: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def pick_ca_bundle(candidates: list[pathlib.Path] | tuple[pathlib.Path, ...] = CA_BUNDLE_CANDIDATES) -> pathlib.Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def build_ssl_context() -> ssl.SSLContext:
    ca_bundle = pick_ca_bundle()
    if ca_bundle is not None:
        return ssl.create_default_context(cafile=str(ca_bundle))
    return ssl.create_default_context()


def post_form(url: str, data: dict[str, str], timeout: int = 30) -> dict[str, Any]:
    encoded = urlencode(data).encode("utf-8")
    request = Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout, context=build_ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError as err:
            raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from err
    except URLError as exc:
        raise RuntimeError(f"Network error while calling {url}: {exc}") from exc


def choose_client_credentials(env: dict[str, str]) -> tuple[str, str, str]:
    client_id = env.get("GOOGLE_DEVICE_CLIENT_ID")
    client_secret = env.get("GOOGLE_DEVICE_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError(
            "Missing headless Google OAuth credentials. "
            "Set GOOGLE_DEVICE_CLIENT_ID and GOOGLE_DEVICE_CLIENT_SECRET in .env."
        )

    return client_id, client_secret, "TVs and Limited Input devices"


def request_device_code(client_id: str, scopes: list[str]) -> dict[str, Any]:
    payload = post_form(
        DEVICE_CODE_URL,
        {
            "client_id": client_id,
            "scope": " ".join(scopes),
        },
    )
    if "device_code" not in payload or "user_code" not in payload:
        raise RuntimeError(f"Unexpected device-code response: {payload}")
    return payload


def build_token_request_data(client_id: str, client_secret: str, device_code: str) -> dict[str, str]:
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": device_code,
        "grant_type": DEVICE_GRANT_TYPE,
    }


def poll_for_token(client_id: str, client_secret: str, device_code: str, interval: int) -> dict[str, Any]:
    poll_interval = max(interval, 1)
    while True:
        time.sleep(poll_interval)
        payload = post_form(TOKEN_URL, build_token_request_data(client_id, client_secret, device_code))

        if "access_token" in payload:
            return payload

        error = payload.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            poll_interval += 5
            continue
        if error == "access_denied":
            raise RuntimeError("Authorization denied by user.")
        if error == "expired_token":
            raise RuntimeError("Device code expired before authorization completed.")

        raise RuntimeError(f"Token exchange failed: {payload}")


def save_token(token_path: pathlib.Path, token_payload: dict[str, Any], client_id: str, client_secret: str) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_payload.setdefault("token_uri", TOKEN_URL)
    token_payload.setdefault("client_id", client_id)
    token_payload.setdefault("client_secret", client_secret)
    with open(token_path, "w", encoding="utf-8") as f:
        json.dump(token_payload, f, indent=2)


def authorize_scope(client_id: str, client_secret: str, scopes: list[str], token_path: pathlib.Path, service_name: str) -> bool:
    print(f"\n{'=' * 72}")
    print(f"{service_name} authorization")
    print("=" * 72)
    print("Open the URL below in a browser on another device and enter the code.")

    device_payload = request_device_code(client_id, scopes)
    verification_url = device_payload.get("verification_url") or device_payload.get("verification_uri")
    print(f"\nVerification URL: {verification_url}")
    print(f"User code:        {device_payload['user_code']}")
    print(f"Expires in:       {device_payload.get('expires_in', 'unknown')} seconds")
    print("\nWaiting for approval...")

    token_payload = poll_for_token(
        client_id=client_id,
        client_secret=client_secret,
        device_code=device_payload["device_code"],
        interval=int(device_payload.get("interval", 5)),
    )
    save_token(token_path, token_payload, client_id, client_secret)
    print(f"Token saved to:   {token_path}")
    return True


def scopes_to_tokens() -> list[tuple[str, list[str], pathlib.Path]]:
    root = pathlib.Path(__file__).resolve().parent.parent
    token_dir = root / "vdirsyncer" / "token"
    return [
        ("Google Calendar", ["https://www.googleapis.com/auth/calendar"], token_dir / "google.json"),
        ("Google Contacts", ["https://www.googleapis.com/auth/contacts"], token_dir / "google_contacts.json"),
    ]


def main() -> None:
    script_dir = pathlib.Path(__file__).resolve().parent
    root = script_dir.parent
    env_file = root / ".env"

    if not env_file.exists():
        print(f"ERROR: .env file not found at {env_file}")
        sys.exit(1)

    env = load_env(env_file)
    client_id, client_secret, client_type = choose_client_credentials(env)

    token_dir = root / "vdirsyncer" / "token"
    token_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("Google OAuth authorization for headless / external-browser use")
    print("=" * 72)
    print(f"OAuth client in use: {client_type}")

    success_count = 0

    configured_services = scopes_to_tokens()

    for service_name, scopes, token_path in configured_services:
        if token_path.exists():
            print(f"{service_name}: token already present at {token_path}")
            success_count += 1
            continue
        try:
            if authorize_scope(client_id, client_secret, scopes, token_path, service_name):
                success_count += 1
        except Exception as exc:
            print(f"ERROR: {service_name} authorization failed: {exc}")

    print(f"\nCompleted: {success_count}/{len(configured_services)} tokens available")
    print(f"Token directory: {token_dir}")

    if success_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
