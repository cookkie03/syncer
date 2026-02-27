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


def load_env(path: pathlib.Path) -> dict:
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


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
    # Use as_posix() — vdirsyncer config parser rejects Windows backslashes.
    token_path = pathlib.Path.home() / "google.json"
    env["GOOGLE_TOKEN_FILE"] = token_path.as_posix()
    env["GOOGLE_CONTACTS_TOKEN_FILE"] = (pathlib.Path.home() / "google_contacts.json").as_posix()

    # ── Render config template ─────────────────────────────────────────────
    with open(template_file, encoding="utf-8") as f:
        content = f.read()

    for k, v in env.items():
        content = content.replace(f"${k}", v)

    # Replace Docker-only status_path with a local temp dir
    local_status = str(pathlib.Path(tempfile.gettempdir()) / "vdirsyncer-status")
    local_status = local_status.replace("\\", "/")
    content = re.sub(
        r'status_path\s*=\s*"/data/status"',
        f'status_path = "{local_status}"',
        content,
    )

    config_path = pathlib.Path(tempfile.gettempdir()) / "vdirsyncer-authorize.cfg"
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

    # vdirsyncer 0.20.x dropped the 'authorize' command.
    # OAuth is now triggered automatically during 'discover' when no token exists.
    result = subprocess.run(
        [sys.executable, "-m", "vdirsyncer", "-c", str(config_path), "discover"],
        env=proc_env,
    )

    if result.returncode != 0:
        print("\nERROR: discover failed — see output above.")
        sys.exit(result.returncode)

    if not token_path.exists():
        print("\nERROR: Token file was not created. Authorization may have been cancelled.")
        sys.exit(1)

    # ── Print the copy command ─────────────────────────────────────────────
    folder = root.name
    volume = f"{folder}_vdirsyncer_token"
    token_str = str(token_path).replace("\\", "/")

    print()
    print("=" * 60)
    print("Authorization successful!")
    print("=" * 60)
    print()
    print("Now copy the token into the Docker volume with this command:")
    print()
    print(f'  docker run --rm \\')
    print(f'    -v {volume}:/data/token \\')
    print(f'    -v "{token_str}:/src/google.json" \\')
    print(f'    alpine cp /src/google.json /data/token/google.json')
    print()
    print("Then start the stack:")
    print()
    print("  docker compose up -d --build")
    print()


if __name__ == "__main__":
    main()
