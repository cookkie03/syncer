#!/usr/bin/env python3
"""Run Todoist backup commands locally using the portable .env, without printing secrets."""

import os
import subprocess
import sys
from pathlib import Path

from project_env import load_project_env


def main():
    root = Path(__file__).resolve().parent.parent
    env_file = root / ".env"
    if not env_file.is_file():
        print("Missing .env; configure TODOIST_API_TOKEN locally", file=sys.stderr)
        return 1
    env = dict(os.environ)
    for key, value in load_project_env(env_file).items():
        if key.startswith("TODOIST_"):
            env[key] = value
    env.setdefault("TODOIST_STATE_DIR", str(root / "todoist-backup" / "state"))
    env.setdefault("TODOIST_BACKUP_DIR", str(root / "todoist-backup" / "backup"))
    return subprocess.call([sys.executable, str(root / "todoist-backup" / "backup.py"), *sys.argv[1:]], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
