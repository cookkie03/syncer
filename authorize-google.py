#!/usr/bin/env python3
"""
One-shot Google OAuth authorization for vdirsyncer.
Run from the project root: python authorize-google.py
"""

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import json

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    HAS_GOOGLE_AUTH = True
except ImportError:
    HAS_GOOGLE_AUTH = False


def load_env(path: pathlib.Path) -> dict:
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def authorize_people_api(client_id, client_secret, token_path):
    print("=" * 60)
    print("Google People API (Contacts) authorization")
    print("=" * 60)
    
    config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    
    scopes = ["https://www.googleapis.com/auth/contacts"]
    flow = InstalledAppFlow.from_client_config(config, scopes)
    creds = flow.run_local_server(port=0)
    
    with open(token_path, "w") as f:
        f.write(creds.to_json())
    
    print(f"Token saved to: {token_path}")
    print()


def main():
    root = pathlib.Path(__file__).parent

    # ── Validate prerequisites ─────────────────────────────────────────────
    env_file = root / ".env"
    template_file = root / "vdirsyncer" / "config.template"

    for f in (env_file, template_file):
        if not f.exists():
            print(f"ERROR: {f} not found. Run this script from the project root.")
            sys.exit(1)

    # ── Load .env ──────────────────────────────────────────────────────────
    env = load_env(env_file)

    required = ["CALDAV_URL", "CALDAV_USERNAME", "CALDAV_PASSWORD",
                "CARDDAV_URL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        print("ERROR: Missing values in .env:", ", ".join(missing))
        sys.exit(1)

    # ── Token output paths (local, not Docker paths) ───────────────────────
    # Save tokens inside the project's vdirsyncer/token folder
    token_dir = root / "vdirsyncer" / "token"
    token_dir.mkdir(parents=True, exist_ok=True)
    
    token_path = token_dir / "google.json"
    contacts_token_path = token_dir / "google_contacts.json"
    
    # Use as_posix() — vdirsyncer config parser rejects Windows backslashes.
    env["GOOGLE_TOKEN_FILE"] = token_path.as_posix()
    env["GOOGLE_CONTACTS_TOKEN_FILE"] = contacts_token_path.as_posix()

    # ── Render config template ─────────────────────────────────────────────
    with open(template_file, encoding="utf-8") as f:
        content = f.read()

    for k, v in env.items():
        content = content.replace(f"${k}", v)

    # Replace Docker-only status_path with a local temp dir inside the project
    local_status = root / "vdirsyncer" / "status"
    local_status.mkdir(parents=True, exist_ok=True)
    content = re.sub(
        r'status_path\s*=\s*"/data/status"',
        f'status_path = "{local_status.as_posix()}"',
        content,
    )

    config_path = root / "vdirsyncer-authorize.cfg"
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(content)

    print("=" * 60)
    print("vdirsyncer Google OAuth authorization")
    print("=" * 60)
    print(f"Token will be saved to: {token_path}")
    print()

    # ── Run vdirsyncer authorize ───────────────────────────────────────────
    proc_env = os.environ.copy()
    proc_env["VDIRSYNCER_CONFIG"] = str(config_path)

    # Find the vdirsyncer executable in the virtual environment
    vdirsyncer_exe = "vdirsyncer"
    if os.name == "nt":
        venv_scripts = pathlib.Path(sys.executable).parent
        possible_exe = venv_scripts / "vdirsyncer.exe"
        if possible_exe.exists():
            vdirsyncer_exe = str(possible_exe)

    # vdirsyncer 0.20.x dropped the 'authorize' command.
    # OAuth is now triggered automatically during 'discover' when no token exists.
    result = subprocess.run(
        [vdirsyncer_exe, "-c", str(config_path), "discover"],
        env=proc_env,
    )

    if result.returncode != 0:
        print("\nERROR: discover failed — see output above.")
        # We continue to contacts auth anyway if possible

    # ── Google People API Auth ────────────────────────────────────────────
    if not contacts_token_path.exists():
        if HAS_GOOGLE_AUTH:
            try:
                authorize_people_api(env["GOOGLE_CLIENT_ID"], env["GOOGLE_CLIENT_SECRET"], contacts_token_path)
            except Exception as e:
                print(f"ERROR: People API authorization failed: {e}")
        else:
            print("\nWARNING: 'google-auth-oauthlib' not found. Skipping Contacts authorization.")
            print("To authorize contacts, run: pip install google-auth-oauthlib")

    # Clean up temp config
    if config_path.exists():
        config_path.unlink()

    if not token_path.exists() and not contacts_token_path.exists():
        print("\nERROR: No token files were created.")
        sys.exit(1)

    print()
    print("=" * 60)
    print("Authorization completed!")
    print("=" * 60)
    print(f"Tokens have been saved directly to: {token_dir}")
    print()
    print("You can now start the stack:")
    print()
    print("  docker compose up -d --build")
    print()


if __name__ == "__main__":
    main()
