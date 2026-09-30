"""The pairing check must block writes when collection IDs cannot be trusted."""

import unittest
from unittest.mock import patch

from refresh_pairing import parse_discover_output, refresh_map, run_discover


class PairingSafetyTests(unittest.TestCase):
    def test_discover_does_not_confirm_collection_creation(self):
        output = 'caldav_calendars:\ngoogle_calendars:\n'
        with patch("refresh_pairing.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = output
            self.assertEqual(run_discover(), output)
            self.assertNotIn("y", run.call_args.kwargs["input"])

    def test_missing_counterpart_does_not_reuse_stale_id(self):
        previous = [{"name": "Shared", "caldav": "old-a", "google": "old-b"}]
        with self.assertRaisesRegex(RuntimeError, "Pairing incomplete"):
            refresh_map(previous, {"Shared": "new-a"}, {})

    def test_duplicate_display_name_is_ambiguous(self):
        output = 'caldav_calendars:\n  - "one" ("Shared")\n  - "two" ("Shared")\n'
        with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
            parse_discover_output(output)

    def test_matching_names_refresh_ids(self):
        previous = [{"name": "Shared", "caldav": "old-a", "google": "old-b"}]
        updated = refresh_map(previous, {"Shared": "new-a"}, {"Shared": "new-b"})
        self.assertEqual(updated, [{"name": "Shared", "caldav": "new-a", "google": "new-b"}])

    def test_unlisted_calendar_is_not_added_to_sync(self):
        previous = [{"name": "Shared", "caldav": "old-a", "google": "old-b"}]
        updated = refresh_map(
            previous,
            {"Shared": "new-a", "Personal": "other-a"},
            {"Shared": "new-b", "Personal": "other-b"},
        )
        self.assertEqual([entry["name"] for entry in updated], ["Shared"])


if __name__ == "__main__":
    unittest.main()
