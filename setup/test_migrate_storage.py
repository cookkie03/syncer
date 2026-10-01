import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("migrate_storage", Path(__file__).with_name("migrate-storage.py"))
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, content="preserve"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_moves_tokens_sync_state_caches_and_backups_without_changing_bytes(self):
        paths = {
            "vdirsyncer/token/google.json": "vdirsyncer/state/token/google.json",
            "vdirsyncer/status/pair.items": "vdirsyncer/state/status/pair.items",
            "vdirsyncer/config/config": "vdirsyncer/state/config/config",
            "caldav-backup/backup/latest/calendar.ics": "caldav-backup/backup/current/calendar.ics",
            "caldav-backup/backup/calendar.ics": "caldav-backup/backup/snapshots/legacy-root/calendar.ics",
            "spotify-backup/data/.cache": "spotify-backup/state/.cache",
            "spotify-backup/data/backup/playlist_track_cache.json": "spotify-backup/state/playlist_track_cache.json",
            "spotify-backup/data/backup/spotify_backup_current.json": "spotify-backup/backup/current/spotify_backup_current.json",
            "spotify-backup/data/backup/snapshots/2026-09-30T120000000001-spotify_backup_current.json":
                "spotify-backup/backup/snapshots/2026-09-30T120000000001Z/spotify_backup_current.json",
            "notion-backup/backup/json/manifest.json": "notion-backup/backup/current/json/manifest.json",
            "notion-backup/backup/.last_notified_msg_id": "notion-backup/state/.last_notified_msg_id",
        }
        for old in paths:
            self.write(old, old)
        todoist = self.write("todoist-backup/data/current.json", "unrelated")
        env = self.write(".env", "private")
        migration.apply_plan(self.root, migration.migration_plan(self.root))
        for old, new in paths.items():
            self.assertEqual((self.root / new).read_text(), old)
        self.assertEqual(todoist.read_text(), "unrelated")
        self.assertEqual(env.read_text(), "private")
        self.assertEqual(migration.migration_plan(self.root), [])

    def test_preview_is_read_only_and_conflict_stops_all_moves(self):
        token = self.write("vdirsyncer/token/google.json")
        cache = self.write("spotify-backup/data/.cache", "old")
        destination = self.write("spotify-backup/state/.cache", "new")
        plan = migration.migration_plan(self.root)
        self.assertTrue(token.exists())
        with self.assertRaisesRegex(RuntimeError, "Destination already exists"):
            migration.apply_plan(self.root, plan)
        self.assertTrue(token.exists())
        self.assertEqual(cache.read_text(), "old")
        self.assertEqual(destination.read_text(), "new")

    def test_contacts_relative_current_link_and_hardlinks_survive(self):
        snapshot = self.write("google-contacts-backup/backup/snapshots/20260930_120000/contacts/a.vcf")
        latest = self.root / "google-contacts-backup/backup/latest"
        latest.symlink_to("snapshots/20260930_120000", target_is_directory=True)
        self.write("google-contacts-backup/backup/latest.json", "metadata")
        inode = snapshot.stat().st_ino
        migration.apply_plan(self.root, migration.migration_plan(self.root))
        current = latest.with_name("current")
        self.assertEqual((current / "contacts/a.vcf").stat().st_ino, inode)
        self.assertEqual(current.readlink(), Path("snapshots/20260930_120000"))

    def test_external_pointer_is_rejected_before_any_move(self):
        self.write("vdirsyncer/token/google.json")
        link = self.root / "google-contacts-backup/backup/latest"
        link.parent.mkdir(parents=True)
        link.symlink_to("/private/tmp")
        with self.assertRaises(RuntimeError):
            migration.apply_plan(self.root, migration.migration_plan(self.root))
        self.assertTrue((self.root / "vdirsyncer/token/google.json").exists())

    def test_running_services_block_migration(self):
        from types import SimpleNamespace
        with patch.object(migration.shutil, "which", return_value="docker"), \
             patch.object(migration.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="container\n")):
            with self.assertRaisesRegex(RuntimeError, "Stop services"):
                migration.ensure_stopped(self.root)


if __name__ == "__main__":
    unittest.main()
