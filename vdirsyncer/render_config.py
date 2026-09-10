#!/usr/bin/env python3
"""Render vdirsyncer config from template and calendar mapping."""

from __future__ import annotations

import json
import os
import pathlib
import sys


TEMPLATE_PATH = pathlib.Path("/app/config.template")
OUTPUT_PATH = pathlib.Path(os.environ.get("VDIRSYNCER_CONFIG_FILE", "/data/vdirsyncer/config"))
CALENDAR_MAP_FILE = pathlib.Path(os.environ.get("CALENDAR_MAP_FILE", "/app/calendar-map.json"))


def load_calendar_map(path: pathlib.Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"Calendar map must be a non-empty JSON list: {path}")

    normalized: list[dict[str, str]] = []
    for idx, entry in enumerate(payload, start=1):
        if not isinstance(entry, dict):
            raise RuntimeError(f"Calendar map entry #{idx} is not an object")
        name = str(entry.get("name", "")).strip()
        caldav_id = str(entry.get("caldav", "")).strip()
        google_id = str(entry.get("google", "")).strip()
        if not name or not caldav_id or not google_id:
            raise RuntimeError(f"Calendar map entry #{idx} must contain name/caldav/google")
        normalized.append({"name": name, "caldav": caldav_id, "google": google_id})
    return normalized


def build_collections_json(entries: list[dict[str, str]]) -> str:
    collections = [[entry["name"], entry["caldav"], entry["google"]] for entry in entries]
    return json.dumps(collections, ensure_ascii=False)


def render_template(template_text: str, collections_json: str) -> str:
    expanded = os.path.expandvars(template_text)
    return expanded.replace("__COLLECTIONS_JSON__", collections_json)


def main() -> int:
    if not TEMPLATE_PATH.is_file():
        raise RuntimeError(f"Missing template: {TEMPLATE_PATH}")
    if not CALENDAR_MAP_FILE.is_file():
        raise RuntimeError(f"Missing calendar map: {CALENDAR_MAP_FILE}")

    entries = load_calendar_map(CALENDAR_MAP_FILE)
    rendered = render_template(TEMPLATE_PATH.read_text(encoding="utf-8"), build_collections_json(entries))
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"[render-config] Wrote {OUTPUT_PATH} with {len(entries)} mapped calendars")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[render-config] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
