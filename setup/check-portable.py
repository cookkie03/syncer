#!/usr/bin/env python3
"""Check that the copied folder contains everything active services need."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vdirsyncer"))
sys.path.insert(0, str(ROOT / "settings"))
from project_env import load_project_env
from render_config import load_calendar_map
from google_auth import CLIENT_FILENAME, load_google_client, token_matches_client

REQUIRED = (
    "CALDAV_URL",
    "CALDAV_USERNAME",
    "CALDAV_PASSWORD",
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_REDIRECT_URI",
    "TODOIST_API_TOKEN",
)


def load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def spotify_token_ready() -> bool:
    try:
        return bool(load_json(ROOT / "spotify-backup" / "state" / ".cache").get("refresh_token"))
    except (OSError, ValueError):
        return False


def check(config_only: bool = False, include_gmail: bool = False) -> list[str]:
    errors: list[str] = []
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return ["Missing .env; copy settings/.env.example and fill active service settings"]
    values = load_project_env(env_file)
    for name in REQUIRED:
        value = values.get(name, "")
        if not value or value == "YOUR_VALUE_HERE" or value.startswith("${"):
            errors.append(f"Missing {name} in .env")

    try:
        client = load_google_client(ROOT / "setup" / "google" / CLIENT_FILENAME)
    except (OSError, ValueError, RuntimeError):
        errors.append("Place a valid Google Desktop client JSON at setup/google/client_secret.json")
        client = None

    map_file = ROOT / "settings" / "calendar-pairings.json"
    try:
        entries = load_calendar_map(map_file)
        if any(str(value).startswith("CHANGE_ME") for entry in entries for value in entry.values()):
            raise ValueError("replace example placeholders")
    except (OSError, ValueError, RuntimeError) as exc:
        errors.append(f"Calendar map missing or invalid: {type(exc).__name__}; see README setup")

    if config_only:
        return errors

    token_dir = ROOT / "vdirsyncer" / "state" / "token"
    token_specs = [
        ("google.json", ("access_token", "refresh_token"), "https://www.googleapis.com/auth/calendar"),
        ("google_contacts.json", ("token", "refresh_token"), "https://www.googleapis.com/auth/contacts"),
    ]
    if include_gmail:
        token_specs.append(("google_gmail.json", ("token", "refresh_token"), "https://www.googleapis.com/auth/gmail.readonly"))
    for filename, fields, scope in token_specs:
        try:
            payload = load_json(token_dir / filename)
            saved_client_id = None
            if filename == "google.json":
                saved_client_id = (token_dir / "google-client-id").read_text(encoding="utf-8").strip()
                if not client or saved_client_id != client["client_id"]:
                    raise ValueError("Calendar token client marker does not match")
                if not (payload.get("scope") or payload.get("scopes")):
                    payload = {**payload, "scope": scope}
            if not client or not token_matches_client(payload, client, fields, scope, saved_client_id):
                raise ValueError("token does not match the current Desktop client and scope")
        except (OSError, ValueError):
            errors.append(f"Missing or incompatible token: vdirsyncer/state/token/{filename}")

    if not spotify_token_ready():
        errors.append("Missing or invalid Spotify cache: spotify-backup/state/.cache")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-only", action="store_true", help="Check setup before browser authorization")
    parser.add_argument("--spotify-ready", action="store_true", help="Check the Spotify refresh token only")
    parser.add_argument("--include-gmail", action="store_true", help="Also check Gmail for the disabled Notion export service")
    args = parser.parse_args()
    if args.spotify_ready:
        return 0 if spotify_token_ready() else 1
    errors = check(config_only=args.config_only, include_gmail=args.include_gmail)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("OK: portable configuration" + ("" if args.config_only else " and OAuth files"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
