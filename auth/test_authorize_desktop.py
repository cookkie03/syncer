"""Protect existing portable OAuth tokens during reauthorization."""

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("authorize-desktop.py")


class DesktopAuthTests(unittest.TestCase):
    def test_replacement_keeps_a_copy_of_previous_token(self):
        spec = importlib.util.spec_from_file_location("authorize_desktop", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "google.json"
            token.write_text("old-token", encoding="utf-8")
            module.save_token(token, "new-token")
            self.assertEqual(token.read_text(encoding="utf-8"), "new-token")
            backups = list(Path(directory).glob("google.json.backup.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "old-token")
            self.assertEqual(token.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
