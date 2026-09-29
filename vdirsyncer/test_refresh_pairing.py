"""The pairing check must block writes when collection IDs cannot be trusted."""

import unittest

from refresh_pairing import parse_discover_output, refresh_map


class PairingSafetyTests(unittest.TestCase):
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
        updated, warnings = refresh_map(previous, {"Shared": "new-a"}, {"Shared": "new-b"})
        self.assertEqual(updated, [{"name": "Shared", "caldav": "new-a", "google": "new-b"}])
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
