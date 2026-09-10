#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parent / "render_config.py"


def load_module():
    spec = importlib.util.spec_from_file_location("render_config", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RenderConfigTests(unittest.TestCase):
    def test_load_calendar_map_requires_fields(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            path.write_text('[{"name":"Amore","caldav":"abc","google":"def"}]', encoding="utf-8")
            payload = module.load_calendar_map(path)
            self.assertEqual(payload[0]["google"], "def")

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


if __name__ == "__main__":
    unittest.main()
