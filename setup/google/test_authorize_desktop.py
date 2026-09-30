"""Protect existing portable OAuth tokens during reauthorization."""

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).with_name("authorize-desktop.py")


class DesktopAuthTests(unittest.TestCase):
    def load_module(self):
        spec = importlib.util.spec_from_file_location("authorize_desktop", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_replacement_keeps_a_copy_of_previous_token(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "google.json"
            token.write_text("old-token", encoding="utf-8")
            module.save_token(token, "new-token")
            self.assertEqual(token.read_text(encoding="utf-8"), "new-token")
            backups = list(Path(directory).glob("google.json.backup.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "old-token")
            self.assertEqual(token.stat().st_mode & 0o777, 0o600)

    def test_calendar_token_uses_vdirsyncer_oauth_fields(self):
        module = self.load_module()
        credentials = SimpleNamespace(
            token="access",
            refresh_token="refresh",
            expiry=datetime(2030, 1, 1, tzinfo=timezone.utc),
            scopes=[module.SERVICES[0][2]],
            client_id="desktop-client",
        )
        payload = json.loads(module.calendar_token(credentials))
        self.assertEqual(payload["access_token"], "access")
        self.assertEqual(payload["refresh_token"], "refresh")
        self.assertEqual(payload["expires_at"], credentials.expiry.timestamp())
        self.assertEqual(payload["client_id"], "desktop-client")

    def test_google_auth_json_is_not_accepted_as_calendar_token(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "google.json"
            path.write_text('{"token":"access","refresh_token":"refresh"}', encoding="utf-8")
            self.assertFalse(module.token_has_required_fields(
                path, "google.json", {"client_id": "desktop-client", "client_secret": "secret"}
            ))

    def test_old_client_token_requests_new_consent(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "google.json"
            path.write_text(json.dumps({
                "access_token": "access", "refresh_token": "refresh",
                "client_id": "deleted-client", "scope": module.SERVICES[0][2],
            }), encoding="utf-8")
            self.assertFalse(module.token_has_required_fields(
                path, "google.json", {"client_id": "desktop-client", "client_secret": "secret"}
            ))


if __name__ == "__main__":
    unittest.main()
