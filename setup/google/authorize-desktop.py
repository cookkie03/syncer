#!/usr/bin/env python3
"""Authorize active Google services in a browser and save portable OAuth tokens."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import tempfile
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TOKEN_DIR = ROOT / "vdirsyncer" / "token"
sys.path.insert(0, str(ROOT / "settings"))
from google_auth import CLIENT_FILENAME, load_google_client, token_matches_client

SERVICES = (
    ("Google Calendar", "google.json", "https://www.googleapis.com/auth/calendar"),
    ("Google Contacts", "google_contacts.json", "https://www.googleapis.com/auth/contacts"),
    ("Gmail for disabled Notion export", "google_gmail.json", "https://www.googleapis.com/auth/gmail.readonly"),
)


def client_file() -> pathlib.Path:
    path = ROOT / "setup" / "google" / CLIENT_FILENAME
    load_google_client(path)
    return path


def save_token(path: pathlib.Path, serialized: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        if path.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup = path.with_name(f"{path.name}.backup.{stamp}")
            shutil.copy2(path, backup)
            backup.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def calendar_token(credentials) -> str:
    """Encode the OAuth2Session format consumed by vdirsyncer's Google storage."""
    if not credentials.token or not credentials.refresh_token:
        raise RuntimeError("Google returned no access or refresh token")
    expiry = credentials.expiry
    if expiry is None:
        raise RuntimeError("Google returned no token expiry")
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    payload = {
        "access_token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_type": "Bearer",
        "expires_at": expiry.timestamp(),
        "scope": " ".join(credentials.scopes or [SERVICES[0][2]]),
        "client_id": credentials.client_id,
    }
    return json.dumps(payload)


def token_has_required_fields(path: pathlib.Path, filename: str, client: dict[str, str]) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    fields = ("access_token", "refresh_token") if filename == "google.json" else ("token", "refresh_token")
    required_scope = next(scope for _, service_file, scope in SERVICES if service_file == filename)
    if filename == "google.json":
        try:
            saved_client_id = path.with_name("google-client-id").read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if saved_client_id != client["client_id"]:
            return False
        if not (payload.get("scope") or payload.get("scopes")):
            payload = {**payload, "scope": required_scope}
        return token_matches_client(payload, client, fields, required_scope, saved_client_id)
    return token_matches_client(payload, client, fields, required_scope)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Reauthorize existing tokens")
    parser.add_argument("--include-gmail", action="store_true", help="Also authorize Gmail for the disabled Notion export service")
    args = parser.parse_args()
    source = client_file()
    client = load_google_client(source)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError("Install google-auth-oauthlib in the Python environment used for this command") from exc

    failures = 0
    for name, filename, scope in SERVICES if args.include_gmail else SERVICES[:2]:
        destination = TOKEN_DIR / filename
        if destination.exists() and not args.force:
            if not token_has_required_fields(destination, filename, client):
                print(f"{name}: existing token is incompatible with the current client; requesting new consent")
            else:
                print(f"{name}: existing token retained at {destination}")
                continue
        print(f"{name}: opening browser for OAuth consent")
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(source), scopes=[scope])
            credentials = flow.run_local_server(
                host="127.0.0.1",
                port=0,
                open_browser=True,
                access_type="offline",
                prompt="consent",
            )
            if not credentials.refresh_token:
                raise RuntimeError("Google returned no refresh token; token was not saved")
            serialized = calendar_token(credentials) if filename == "google.json" else credentials.to_json()
            save_token(destination, serialized)
            if filename == "google.json":
                marker = destination.with_name("google-client-id")
                marker.write_text(client["client_id"], encoding="utf-8")
                marker.chmod(0o600)
            print(f"{name}: token saved at {destination}")
        except Exception as exc:
            failures += 1
            print(f"{name}: authorization failed: {exc}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
