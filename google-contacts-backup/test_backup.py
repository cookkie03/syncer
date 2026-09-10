#!/usr/bin/env python3
"""Tests for incremental Google Contacts backups."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "backup.py"


def load_backup_module(testcase: unittest.TestCase):
    if not MODULE_PATH.exists():
        testcase.fail(f"Missing backup module: {MODULE_PATH}")

    spec = importlib.util.spec_from_file_location("contacts_backup", MODULE_PATH)
    if spec is None or spec.loader is None:
        testcase.fail(f"Could not load backup module from: {MODULE_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IncrementalBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="contacts_backup_test_")
        self.base = Path(self.tmpdir.name)
        self.backup_dir = self.base / "backup"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_reuses_hardlinks_for_unchanged_contacts(self):
        module = load_backup_module(self)

        previous = module.write_incremental_snapshot(
            backup_root=self.backup_dir,
            contacts={
                "uid-1": "BEGIN:VCARD\nFN:Alice\nEND:VCARD\n",
                "uid-2": "BEGIN:VCARD\nFN:Bob\nEND:VCARD\n",
            },
            timestamp="20260717_120000",
        )

        current = module.write_incremental_snapshot(
            backup_root=self.backup_dir,
            contacts={
                "uid-1": "BEGIN:VCARD\nFN:Alice\nEND:VCARD\n",
                "uid-2": "BEGIN:VCARD\nFN:Bob Updated\nEND:VCARD\n",
                "uid-3": "BEGIN:VCARD\nFN:Carol\nEND:VCARD\n",
            },
            timestamp="20260717_130000",
        )

        prev_file = Path(previous["snapshot_dir"]) / "contacts" / "uid-1.vcf"
        current_file = Path(current["snapshot_dir"]) / "contacts" / "uid-1.vcf"
        self.assertTrue(prev_file.exists())
        self.assertTrue(current_file.exists())
        self.assertEqual(os.stat(prev_file).st_ino, os.stat(current_file).st_ino)

        changed_prev = Path(previous["snapshot_dir"]) / "contacts" / "uid-2.vcf"
        changed_current = Path(current["snapshot_dir"]) / "contacts" / "uid-2.vcf"
        self.assertNotEqual(os.stat(changed_prev).st_ino, os.stat(changed_current).st_ino)

        self.assertEqual(current["stats"]["new"], 1)
        self.assertEqual(current["stats"]["changed"], 1)
        self.assertEqual(current["stats"]["unchanged"], 1)
        self.assertEqual(current["stats"]["deleted"], 0)

    def test_reports_deleted_contacts_and_updates_latest_pointer(self):
        module = load_backup_module(self)

        module.write_incremental_snapshot(
            backup_root=self.backup_dir,
            contacts={
                "uid-1": "BEGIN:VCARD\nFN:Alice\nEND:VCARD\n",
                "uid-2": "BEGIN:VCARD\nFN:Bob\nEND:VCARD\n",
            },
            timestamp="20260717_120000",
        )

        current = module.write_incremental_snapshot(
            backup_root=self.backup_dir,
            contacts={
                "uid-1": "BEGIN:VCARD\nFN:Alice\nEND:VCARD\n",
            },
            timestamp="20260717_130000",
        )

        deleted_file = Path(current["snapshot_dir"]) / "contacts" / "uid-2.vcf"
        self.assertFalse(deleted_file.exists())
        self.assertEqual(current["stats"]["deleted"], 1)
        self.assertEqual(current["stats"]["total"], 1)

        latest_path = self.backup_dir / "latest.json"
        self.assertTrue(latest_path.exists())
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        self.assertEqual(latest["snapshot"], "20260717_130000")

    def test_reads_scope_string_from_google_token_payload(self):
        module = load_backup_module(self)
        scopes = module.google_contacts_scopes_from_token_payload(
            {"scope": "https://www.googleapis.com/auth/contacts"}
        )
        self.assertEqual(scopes, ["https://www.googleapis.com/auth/contacts"])

    def test_defaults_to_contacts_scope_when_token_has_no_scope_field(self):
        module = load_backup_module(self)
        scopes = module.google_contacts_scopes_from_token_payload({})
        self.assertEqual(scopes, ["https://www.googleapis.com/auth/contacts"])


if __name__ == "__main__":
    unittest.main()
