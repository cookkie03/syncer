#!/usr/bin/env python3
"""Tests for spotify-backup auth helper."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "auth_helper.py"


def load_auth_helper_module(testcase: unittest.TestCase):
    if not MODULE_PATH.exists():
        testcase.fail(f"Missing auth helper module: {MODULE_PATH}")

    spec = importlib.util.spec_from_file_location("spotify_auth_helper", MODULE_PATH)
    if spec is None or spec.loader is None:
        testcase.fail(f"Could not load auth helper module from: {MODULE_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SpotifyAuthHelperTests(unittest.TestCase):
    def test_default_cache_path_lives_inside_repo_data_dir(self):
        module = load_auth_helper_module(self)
        self.assertTrue(hasattr(module, "PROJECT_DIR"))
        self.assertTrue(hasattr(module, "default_cache_path"))
        expected = module.PROJECT_DIR / "data" / ".cache"
        self.assertEqual(module.default_cache_path(), expected)

    def test_auth_helper_does_not_open_local_browser_by_default(self):
        module = load_auth_helper_module(self)
        self.assertTrue(hasattr(module, "should_open_browser"))
        with patch.object(module.webbrowser, "open") as mocked_open:
            self.assertFalse(module.should_open_browser())
        mocked_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
