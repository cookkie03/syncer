#!/usr/bin/env python3
"""Tests for GitHub mirror discovery and deletion safety."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("backup.py")


def load_module():
    os.environ["LOG_DIR"] = tempfile.mkdtemp(prefix="github_backup_logs_")
    spec = importlib.util.spec_from_file_location("github_backup", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GitHubBackupTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name)
        self.module.BACKUP_DIR = base / "backup"
        self.module.STATE_DIR = base / "state"
        self.module.MIRRORS_DIR = self.module.BACKUP_DIR / "mirrors"
        self.module.STATUS_PATH = self.module.STATE_DIR / "status.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_discovery_deduplicates_owned_and_starred_repositories(self):
        responses = {
            ("/user", 1): {"login": "me"},
            ("/user/repos?affiliation=owner&sort=full_name", 1): [
                {"full_name": "me/owned", "clone_url": "https://github.com/me/owned.git"}
            ],
            ("/user/starred?sort=created", 1): [
                {"full_name": "me/owned", "clone_url": "https://github.com/me/owned.git"},
                {"full_name": "other/starred", "clone_url": "https://github.com/other/starred.git"},
            ],
        }
        with patch.object(
            self.module,
            "github_request",
            side_effect=lambda path, token, page=1: responses[(path, page)],
        ):
            repositories, login = self.module.discover_repositories("secret")
        self.assertEqual(login, "me")
        self.assertEqual(set(repositories), {"me/owned", "other/starred"})
        self.assertTrue(repositories["me/owned"]["owned"])
        self.assertTrue(repositories["other/starred"]["starred"])

    def test_cleanup_blocks_more_than_half_and_preserves_mirrors(self):
        self.module.REMOVE_STALE = True
        self.module.MAX_DELETION_RATIO = 0.5
        for name in ("a", "b", "c", "d"):
            path = self.module.MIRRORS_DIR / f"{name}.git"
            path.mkdir(parents=True, exist_ok=True)
        status = {}
        with self.assertLogs(self.module.logger, level="ERROR") as captured:
            self.module.cleanup_stale({"a", "b", "c"}, {"a", "b", "c", "d"}, status)
        self.assertEqual(status["cleanup"]["state"], "blocked")
        self.assertTrue((self.module.MIRRORS_DIR / "a.git").exists())
        self.assertIn("BLOCKED stale mirror cleanup", "\n".join(captured.output))

    def test_status_is_written_atomically(self):
        self.module.save_status({"state": "complete"})
        self.assertEqual(
            json.loads(self.module.STATUS_PATH.read_text(encoding="utf-8"))["state"],
            "complete",
        )


if __name__ == "__main__":
    unittest.main()
