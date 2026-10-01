"""Portable setup must detect incomplete OAuth state before NAS transfer."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check-portable.py")


def load_module():
    spec = importlib.util.spec_from_file_location("check_portable", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PortableCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.module = load_module()
        self.module.ROOT = self.root
        for folder in ("setup/google", "settings", "vdirsyncer/state/token", "spotify-backup/state"):
            (self.root / folder).mkdir(parents=True)
        (self.root / ".env").write_text(
            "\n".join(
                (
                    "CALDAV_URL=https://example.invalid/caldav.php/",
                    "CALDAV_USERNAME=user",
                    "CALDAV_PASSWORD=password",
                    "SPOTIFY_CLIENT_ID=spotify-id",
                    "SPOTIFY_CLIENT_SECRET=spotify-secret",
                    "SPOTIFY_REDIRECT_URI=http://127.0.0.1:9000/callback",
                )
            ),
            encoding="utf-8",
        )
        (self.root / "setup/google/client_secret.json").write_text(
            json.dumps({"installed": {"client_id": "client-id", "client_secret": "client-secret"}}),
            encoding="utf-8",
        )
        (self.root / "settings/calendar-pairings.json").write_text(
            '[{"name":"Home","caldav":"a","google":"b"}]', encoding="utf-8"
        )
        (self.root / "vdirsyncer/state/token/google.json").write_text(
            '{"access_token":"a","refresh_token":"r","client_id":"client-id",'
            '"scope":"https://www.googleapis.com/auth/calendar"}', encoding="utf-8"
        )
        (self.root / "vdirsyncer/state/token/google-client-id").write_text("client-id", encoding="utf-8")
        (self.root / "vdirsyncer/state/token/google_contacts.json").write_text(
            '{"token":"a","refresh_token":"r","client_id":"client-id",'
            '"client_secret":"client-secret","scope":"https://www.googleapis.com/auth/contacts"}', encoding="utf-8"
        )
        (self.root / "spotify-backup/state/.cache").write_text(
            '{"refresh_token":"r"}', encoding="utf-8"
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_complete_folder_passes(self):
        self.assertEqual(self.module.check(), [])
        self.assertTrue(self.module.spotify_token_ready())

    def test_google_client_json_is_single_source(self):
        self.assertEqual(self.module.check(config_only=True), [])

    def test_token_from_another_client_is_rejected(self):
        path = self.root / "vdirsyncer/state/token/google.json"
        payload = json.loads(path.read_text())
        payload["client_id"] = "deleted-client"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertTrue(any("google.json" in error for error in self.module.check()))

    def test_calendar_token_remains_valid_if_sync_removes_client_metadata(self):
        path = self.root / "vdirsyncer/state/token/google.json"
        payload = json.loads(path.read_text())
        del payload["client_id"]
        del payload["scope"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.module.check(), [])

    def test_gmail_is_optional_until_requested(self):
        self.assertEqual(self.module.check(), [])
        self.assertTrue(any("google_gmail.json" in error for error in self.module.check(include_gmail=True)))
        (self.root / "vdirsyncer/state/token/google_gmail.json").write_text(
            '{"token":"a","refresh_token":"r","client_id":"client-id",'
            '"client_secret":"client-secret","scope":"https://www.googleapis.com/auth/gmail.readonly"}',
            encoding="utf-8",
        )
        self.assertEqual(self.module.check(include_gmail=True), [])

    def test_missing_token_is_detected_without_secret_values(self):
        (self.root / "vdirsyncer/state/token/google.json").unlink()
        errors = self.module.check()
        self.assertTrue(any("google.json" in error for error in errors))
        self.assertNotIn("client-secret", " ".join(errors))

    def test_example_calendar_map_is_rejected(self):
        (self.root / "settings/calendar-pairings.json").write_text(
            '[{"name":"CHANGE_ME","caldav":"a","google":"b"}]', encoding="utf-8"
        )
        self.assertTrue(any("Calendar map" in error for error in self.module.check(config_only=True)))


if __name__ == "__main__":
    unittest.main()
