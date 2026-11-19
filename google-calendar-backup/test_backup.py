import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("backup.py")
SPEC = importlib.util.spec_from_file_location("google_calendar_backup", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GoogleCalendarBackupTests(unittest.TestCase):
    def test_event_conversion_preserves_calendar_fields(self):
        ics = MODULE.event_to_ics({
            "id": "event-1",
            "iCalUID": "uid-1",
            "summary": "Team, sync",
            "description": "Line one\nLine two",
            "start": {"dateTime": "2026-11-13T10:00:00+01:00"},
            "end": {"dateTime": "2026-11-13T11:00:00+01:00"},
            "recurrence": ["RRULE:FREQ=WEEKLY"],
        })
        self.assertIn("UID:uid-1", ics)
        self.assertIn("SUMMARY:Team\\, sync", ics)
        self.assertIn("DESCRIPTION:Line one\\nLine two", ics)
        self.assertIn("DTSTART:20261113T090000Z", ics)
        self.assertIn("RRULE:FREQ=WEEKLY", ics)

    def test_snapshot_publishes_manifest_and_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = (MODULE.BACKUP_DIR, MODULE.RETENTION)
            MODULE.BACKUP_DIR = root
            MODULE.RETENTION = 2
            try:
                manifest = MODULE.write_snapshot([
                    ({"id": "primary", "summary": "Primary"}, [{"id": "e1", "summary": "One"}])
                ])
                current = root / "current"
                self.assertEqual(manifest["stats"]["events"], 1)
                self.assertEqual(MODULE.verify_snapshot(current)["stats"]["calendars"], 1)
                self.assertTrue((current / "COMPLETE").exists())
                json.loads((current / "manifest.json").read_text())
            finally:
                MODULE.BACKUP_DIR, MODULE.RETENTION = original


if __name__ == "__main__":
    unittest.main()
