#!/usr/bin/env python3
"""Run Notion locally; load only Notion settings and keep tests off NAS backups."""
import os
import subprocess
import sys
from pathlib import Path
from project_env import load_project_env


def main():
    root = Path(__file__).resolve().parent.parent
    args = sys.argv[1:]
    env_file = root / ".env"
    env = dict(os.environ)
    if env_file.exists():
        for key, value in load_project_env(env_file).items():
            if key.startswith("NOTION_"):
                env.setdefault(key, value)
    # Separate directory, never the mounted production backup.
    env.setdefault("NOTION_BACKUP_DIR", "/private/tmp/syncer-notion-test/backup")
    return subprocess.call([sys.executable, str(root / "notion-backup/backup.py"), *args], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
