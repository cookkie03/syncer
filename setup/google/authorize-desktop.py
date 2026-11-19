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
from urllib.parse import parse_qs, urlparse

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TOKEN_DIR = ROOT / "settings" / "auth" / "google"
sys.path.insert(0, str(ROOT / "settings"))
from google_auth import CLIENT_FILENAME, load_google_client, token_matches_client

SERVICES = (
    ("Google Calendar", "google.json", "https://www.googleapis.com/auth/calendar"),
    ("Google Contacts", "google_contacts.json", "https://www.googleapis.com/auth/contacts"),
    ("Gmail for disabled Notion export", "google_gmail.json", "https://www.googleapis.com/auth/gmail.readonly"),
)


def client_file() -> pathlib.Path:
    for candidate in (
        ROOT / "settings" / "auth" / "google" / CLIENT_FILENAME,
        ROOT / "setup" / "google" / CLIENT_FILENAME,
    ):
        if candidate.is_file():
            load_google_client(candidate)
            return candidate
    path = ROOT / "settings" / "auth" / "google" / CLIENT_FILENAME
    load_google_client(path)
    return path


def extract_code(raw: str) -> str:
    raw = raw.strip()
    if "code=" in raw:
        if "://" not in raw:
            raw = "http://" + raw
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        if "code" in qs:
            return qs["code"][0]
    return raw


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


def calendar_token(credentials, client: dict[str, str] | None = None) -> str:
    """Encode the OAuth2Session format consumed by vdirsyncer and Google backup services."""
    if not credentials.token or not credentials.refresh_token:
        raise RuntimeError("Google returned no access or refresh token")
    expiry = credentials.expiry
    if expiry is None:
        raise RuntimeError("Google returned no token expiry")
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    scopes = credentials.scopes or [SERVICES[0][2], SERVICES[1][2]]
    payload = {
        "token": credentials.token,
        "access_token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_type": "Bearer",
        "token_uri": "https://oauth2.googleapis.com/token",
        "expires_at": expiry.timestamp(),
        "scope": " ".join(scopes),
        "client_id": credentials.client_id,
    }
    if client and client.get("client_secret"):
        payload["client_secret"] = client["client_secret"]
    return json.dumps(payload, indent=2)


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
    parser.add_argument("--port", type=int, default=8090, help="Local server port for OAuth callback (default: 8090)")
    parser.add_argument("--no-browser", "--manual", dest="no_browser", action="store_true",
                        help="Modalita in due passaggi per macchine senza browser")
    args = parser.parse_args()
    source = client_file()
    client = load_google_client(source)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError("Install google-auth-oauthlib in the Python environment used for this command") from exc

    unified_scopes = [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/contacts",
    ]
    if args.include_gmail:
        unified_scopes.append("https://www.googleapis.com/auth/gmail.readonly")

    destination = TOKEN_DIR / "google.json"
    marker = destination.with_name("google-client-id")
    contacts_dest = TOKEN_DIR / "google_contacts.json"

    needs_auth = True
    if destination.exists() and not args.force:
        try:
            payload = json.loads(destination.read_text(encoding="utf-8"))
            saved_id = marker.read_text(encoding="utf-8").strip() if marker.is_file() else None
            has_cal = token_matches_client(payload, client, ("access_token", "refresh_token"), unified_scopes[0], saved_id)
            has_con = token_matches_client(payload, client, ("access_token", "refresh_token"), unified_scopes[1], saved_id)
            if has_cal and has_con:
                print(f"Google: existing unified token retained at {destination}")
                needs_auth = False
        except Exception:
            needs_auth = True

    if not needs_auth:
        return 0

    name = "Google (Calendar & Contacts)"
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(source), scopes=unified_scopes)
        credentials = None
        if not args.no_browser:
            try:
                credentials = flow.run_local_server(
                    host="127.0.0.1",
                    port=args.port,
                    open_browser=True,
                    access_type="offline",
                    prompt="consent",
                )
            except Exception as exc:
                err_msg = str(exc).lower()
                if "browser" in err_msg or "display" in err_msg or "x11" in err_msg:
                    print(f"[{name}] No local browser detected. Switching to two-step manual flow...")
                    credentials = None
                else:
                    raise

        if credentials is None:
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
            flow = InstalledAppFlow.from_client_secrets_file(str(source), scopes=unified_scopes)
            flow.redirect_uri = f"http://127.0.0.1:{args.port}/"
            auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
            print("\n" + "=" * 60)
            print(f"[{name}] STEP 1: Open this URL in any browser (PC, phone, etc.):")
            print(auth_url)
            print("=" * 60)
            print(f"[{name}] STEP 2: Log in and allow access on Google (Calendar + Contacts).")
            print(f"The browser will redirect to http://127.0.0.1:{args.port}/?code=... (showing an error page; this is normal).")
            print("Copy the ENTIRE URL from your browser address bar (or just the code value) and paste it below:")
            print("=" * 60)
            user_input = input(f"[{name}] Paste redirect URL or code here: ").strip()
            code = extract_code(user_input)
            flow.fetch_token(code=code)
            credentials = flow.credentials

        if not credentials.refresh_token:
            raise RuntimeError("Google returned no refresh token; token was not saved")
        serialized = calendar_token(credentials, client)
        save_token(destination, serialized)
        marker.write_text(client["client_id"], encoding="utf-8")
        marker.chmod(0o600)
        save_token(contacts_dest, serialized)
        print(f"Google: unified token saved at {destination} (and mirrored to {contacts_dest})")
    except Exception as exc:
        print(f"{name}: authorization failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
