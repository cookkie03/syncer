#!/usr/bin/env python3
"""Tests for spotify-backup mirror behavior."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "backup.py"


def load_backup_module(testcase: unittest.TestCase):
    if not MODULE_PATH.exists():
        testcase.fail(f"Missing backup module: {MODULE_PATH}")

    fake_spotipy = types.ModuleType("spotipy")
    fake_spotipy.Spotify = object

    fake_oauth = types.ModuleType("spotipy.oauth2")

    class FakeSpotifyOAuth:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    fake_oauth.SpotifyOAuth = FakeSpotifyOAuth

    spec = importlib.util.spec_from_file_location("spotify_backup", MODULE_PATH)
    if spec is None or spec.loader is None:
        testcase.fail(f"Could not load backup module from: {MODULE_PATH}")

    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {"spotipy": fake_spotipy, "spotipy.oauth2": fake_oauth},
    ):
        spec.loader.exec_module(module)
    return module


class SpotifyBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="spotify_backup_test_")
        self.base = Path(self.tmpdir.name)
        self.backup_dir = self.base / "backup"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_save_backup_replaces_single_current_snapshot_without_history(self):
        module = load_backup_module(self)
        module.BACKUP_DIR = str(self.backup_dir)

        first = module.save_backup({"timestamp": "2026-07-17T10:00:00"})
        second = module.save_backup({"timestamp": "2026-07-17T14:00:00"})

        self.assertEqual(first, str(self.backup_dir / "spotify_backup_current.json"))
        self.assertEqual(second, first)
        self.assertEqual(
            sorted(path.name for path in self.backup_dir.iterdir()),
            ["spotify_backup_current.json"],
        )

    def test_main_returns_failure_when_oauth_cache_is_missing(self):
        module = load_backup_module(self)
        module.CLIENT_ID = "client"
        module.CLIENT_SECRET = "secret"
        module.CACHE_PATH = str(self.base / ".cache")
        result = "raised"

        with patch.object(module, "get_spotify_client", side_effect=RuntimeError("client should not be built")):
            try:
                with self.assertLogs(module.logger, level="ERROR") as captured:
                    result = module.main()
            except RuntimeError:
                captured = None

        self.assertEqual(result, 1)
        self.assertIsNotNone(captured)
        self.assertIn("OAuth cache", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
