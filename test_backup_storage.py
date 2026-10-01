import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backup_storage as storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "backup"

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, name, text="complete", retention=14):
        staging = storage.new_staging(self.root)
        (staging / "payload").write_text(text)
        return storage.publish_snapshot(staging, self.root, retention, name)

    def test_current_is_same_content_as_snapshot_without_another_copy(self):
        current = self.publish("2026-10-01T010000Z")
        self.assertTrue(current.is_symlink())
        self.assertFalse(current.readlink().is_absolute())
        snapshot = self.root / "snapshots/2026-10-01T010000Z/payload"
        self.assertEqual((current / "payload").stat().st_ino, snapshot.stat().st_ino)

    def test_retention_one_keeps_current_even_if_clock_moves_backwards(self):
        self.publish("2026-10-02T010000Z", "old")
        self.publish("2026-10-01T010000Z", "new", retention=1)
        self.assertEqual((self.root / "current/payload").read_text(), "new")
        self.assertEqual(len(list((self.root / "snapshots").iterdir())), 1)

    def test_failed_pointer_update_preserves_previous_current(self):
        self.publish("2026-10-01T010000Z", "previous")
        with patch.object(storage.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.publish("2026-10-01T020000Z", "next")
        self.assertEqual((self.root / "current/payload").read_text(), "previous")

    def test_failed_pointer_update_restores_migrated_physical_current(self):
        (self.root / "current").mkdir(parents=True)
        (self.root / "current/payload").write_text("previous")
        with patch.object(storage.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.publish("2026-10-01T020000Z", "next")
        self.assertEqual((self.root / "current/payload").read_text(), "previous")

    def test_collision_does_not_replace_snapshot_or_current(self):
        self.publish("2026-10-01T010000Z", "previous")
        with self.assertRaises(FileExistsError):
            self.publish("2026-10-01T010000Z", "next")
        self.assertEqual((self.root / "current/payload").read_text(), "previous")

    def test_legacy_archives_survive_normal_retention(self):
        legacy = self.root / "snapshots/legacy-root"
        legacy.mkdir(parents=True)
        (legacy / "payload").write_text("legacy")
        self.publish("2026-10-01T010000Z", retention=1)
        self.assertEqual((legacy / "payload").read_text(), "legacy")

    def test_retention_sorts_both_old_and_new_contact_timestamp_formats(self):
        self.publish("20260930_120000")
        self.publish("2026-10-01T010000Z")
        self.publish("2026-10-02T010000Z", retention=2)
        self.assertFalse((self.root / "snapshots/20260930_120000").exists())


if __name__ == "__main__":
    unittest.main()
