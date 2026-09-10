#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parent / "sync_filters.py"


def load_module():
    spec = importlib.util.spec_from_file_location("sync_filters", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SyncFiltersTests(unittest.TestCase):
    def test_extract_summary_returns_human_name(self):
        module = load_module()
        raw = "BEGIN:VEVENT\nSUMMARY:Casa Mati\\, Milano\nEND:VEVENT\n"
        self.assertEqual(module.extract_summary(raw), "Casa Mati, Milano")

    def test_skippable_google_write_error_matches_403_google_caldav(self):
        module = load_module()
        message = "403, message='Forbidden', url=URL('https://apidata.googleusercontent.com/caldav/v2/foo/events/bar.ics')"
        self.assertTrue(module.is_skippable_google_write_error("google_calendars", message))

    def test_skippable_google_write_error_ignores_non_google_storage(self):
        module = load_module()
        message = "403, message='Forbidden', url=URL('https://apidata.googleusercontent.com/caldav/v2/foo/events/bar.ics')"
        self.assertFalse(module.is_skippable_google_write_error("caldav_calendars", message))


if __name__ == "__main__":
    unittest.main()
