#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parent / "render_config.py"


def load_module():
    spec = importlib.util.spec_from_file_location("render_config", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RenderConfigTests(unittest.TestCase):
    def test_desktop_client_json_is_the_config_source(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            client_file = folder / "client_secret.json"
            client_file.write_text(
                json.dumps({"installed": {"client_id": "id", "client_secret": "secret"}}),
                encoding="utf-8",
            )
            self.assertEqual(module.load_google_client(client_file), {"client_id": "id", "client_secret": "secret"})
            client_file.write_text('{"web": {"client_id": "id", "client_secret": "secret"}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Desktop"):
                module.load_google_client(client_file)

    def test_load_calendar_map_requires_fields(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            path.write_text('[{"name":"Amore","caldav":"abc","google":"def"}]', encoding="utf-8")
            payload = module.load_calendar_map(path)
            self.assertEqual(payload[0]["google"], "def")

    def test_duplicate_calendar_names_are_rejected(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            path.write_text(
                '[{"name":"Home","caldav":"a","google":"b"},'
                '{"name":"Home","caldav":"c","google":"d"}]',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "Duplicate"):
                module.load_calendar_map(path)

    def test_build_collections_json_keeps_order(self):
        module = load_module()
        payload = [
            {"name": "Amore", "caldav": "abc", "google": "def"},
            {"name": "Extra", "caldav": "ghi", "google": "jkl"},
        ]
        rendered = module.build_collections_json(payload)
        self.assertEqual(rendered, '[["Amore", "abc", "def"], ["Extra", "ghi", "jkl"]]')

    def test_render_template_replaces_collections_placeholder(self):
        module = load_module()
        template = 'collections = __COLLECTIONS_JSON__\nconflict_resolution = "a wins"\n'
        rendered = module.render_template(template, '[["Amore","abc","def"]]')
        self.assertIn('collections = [["Amore","abc","def"]]', rendered)

    def test_render_template_escapes_password_for_config(self):
        module = load_module()
        with patch.dict(os.environ, {"CALDAV_PASSWORD": 'quoted"and\\slash'}):
            rendered = module.render_template('password = "$CALDAV_PASSWORD"', '[]')
        self.assertEqual(rendered, 'password = "quoted\\"and\\\\slash"')


if __name__ == "__main__":
    unittest.main()
