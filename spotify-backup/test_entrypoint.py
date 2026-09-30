#!/usr/bin/env python3
"""Tests for spotify-backup entrypoint behavior."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "spotify-backup" / "entrypoint.sh"


class SpotifyEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="spotify_entrypoint_test_")
        self.base = Path(self.tmpdir.name)
        self.bin_dir = self.base / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir = self.base / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.write_executable(
            self.bin_dir / "python",
            "#!/bin/sh\nprintf 'fake backup run\\n'\nexit 0\n",
        )
        self.write_executable(
            self.bin_dir / "supercronic",
            "#!/bin/sh\nprintf 'supercronic %s\\n' \"$1\"\nexit 0\n",
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def write_executable(self, path: Path, content: str):
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def test_entrypoint_persists_logs_and_schedules_four_hour_interval(self):
        minimal_env = {
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "LOG_DIR": str(self.logs_dir),
            "SPOTIFY_BACKUP_SCHEDULE": "0 */4 * * *",
            "BACKUP_DIR": str(self.base / "data" / "backup"),
            "SPOTIFY_CLIENT_ID": "client",
            "SPOTIFY_CLIENT_SECRET": "secret",
            "SPOTIFY_REDIRECT_URI": "http://127.0.0.1:9000/callback",
        }

        completed = subprocess.run(
            ["/bin/bash", str(ENTRYPOINT)],
            cwd=REPO_ROOT,
            env=minimal_env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Scheduling backup with expression: 0 */4 * * *", completed.stdout)

        log_file = self.logs_dir / "spotify-backup.log"
        self.assertTrue(log_file.exists())
        self.assertIn("Running initial backup", log_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
