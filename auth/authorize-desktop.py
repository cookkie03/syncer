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


ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKEN_DIR = ROOT / "vdirsyncer" / "token"
SERVICES = (
    ("Google Calendar", "google.json", "https://www.googleapis.com/auth/calendar"),
    ("Google Contacts", "google_contacts.json", "https://www.googleapis.com/auth/contacts"),
)


def client_file(explicit: str | None) -> pathlib.Path:
    if explicit:
        path = pathlib.Path(explicit).expanduser().resolve()
    else:
        candidates = sorted((ROOT / "auth").glob("client_secret*.json"))
        if len(candidates) != 1:
            raise RuntimeError("Specify exactly one Desktop OAuth client with --client-json")
        path = candidates[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "installed" not in payload:
        raise RuntimeError("Google client JSON must be of type Desktop app (installed)")
    return path


def check_compose_client(source: pathlib.Path) -> None:
    payload = json.loads(source.read_text(encoding="utf-8"))["installed"]
    env_path = ROOT / ".env"
    if not env_path.exists():
        raise RuntimeError("Missing .env in project root")
    values = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for field, env_name in (
        ("client_id", "GOOGLE_DEVICE_CLIENT_ID"),
        ("client_secret", "GOOGLE_DEVICE_CLIENT_SECRET"),
    ):
        if payload.get(field) != values.get(env_name):
            raise RuntimeError(f"{env_name} in .env must match the Desktop client JSON before authorization")


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-json", help="Google Desktop OAuth client JSON")
    parser.add_argument("--force", action="store_true", help="Reauthorize existing tokens")
    args = parser.parse_args()
    source = client_file(args.client_json)
    check_compose_client(source)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError("Install google-auth-oauthlib in the Python environment used for this command") from exc

    failures = 0
    for name, filename, scope in SERVICES:
        destination = TOKEN_DIR / filename
        if destination.exists() and not args.force:
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
            save_token(destination, credentials.to_json())
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
