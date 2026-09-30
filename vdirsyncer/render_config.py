#!/usr/bin/env python3
"""Render vdirsyncer config from template and calendar mapping."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

for shared_path in ("/app/project-settings", str(pathlib.Path(__file__).resolve().parent.parent / "settings")):
    if shared_path not in sys.path:
        sys.path.insert(0, shared_path)
from google_auth import load_google_client


TEMPLATE_PATH = pathlib.Path("/app/config.template")
OUTPUT_PATH = pathlib.Path(os.environ.get("VDIRSYNCER_CONFIG_FILE", "/data/vdirsyncer/config"))
CALENDAR_MAP_FILE = pathlib.Path(os.environ.get("CALENDAR_MAP_FILE", "/app/project-settings/calendar-pairings.json"))
GOOGLE_CLIENT_JSON_FILE = pathlib.Path(os.environ.get("GOOGLE_CLIENT_JSON_FILE", "/run/syncer-google-client/client_secret.json"))


def load_calendar_map(path: pathlib.Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"Calendar map must be a non-empty JSON list: {path}")

    normalized: list[dict[str, str]] = []
    names: set[str] = set()
    for idx, entry in enumerate(payload, start=1):
        if not isinstance(entry, dict):
            raise RuntimeError(f"Calendar map entry #{idx} is not an object")
        name = str(entry.get("name", "")).strip()
        caldav_id = str(entry.get("caldav", "")).strip()
        google_id = str(entry.get("google", "")).strip()
        if not name or not caldav_id or not google_id:
            raise RuntimeError(f"Calendar map entry #{idx} must contain name/caldav/google")
        if name in names:
            raise RuntimeError(f"Duplicate calendar name in map: {name}")
        names.add(name)
        normalized.append({"name": name, "caldav": caldav_id, "google": google_id})
    return normalized


def build_collections_json(entries: list[dict[str, str]]) -> str:
    collections = [[entry["name"], entry["caldav"], entry["google"]] for entry in entries]
    return json.dumps(collections, ensure_ascii=False)


def render_template(template_text: str, collections_json: str) -> str:
    rendered = template_text.replace("__COLLECTIONS_JSON__", collections_json)
    for name in (
        "CALDAV_URL", "CALDAV_USERNAME", "CALDAV_PASSWORD", "GOOGLE_TOKEN_FILE",
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
    ):
        marker = f'"${name}"'
        if marker in rendered:
            value = os.environ.get(name)
            if not value:
                raise RuntimeError(f"Missing required config setting: {name}")
            rendered = rendered.replace(marker, json.dumps(value))
    return rendered


def main() -> int:
    if not TEMPLATE_PATH.is_file():
        raise RuntimeError(f"Missing template: {TEMPLATE_PATH}")
    if not CALENDAR_MAP_FILE.is_file():
        raise RuntimeError(f"Missing calendar map: {CALENDAR_MAP_FILE}")

    entries = load_calendar_map(CALENDAR_MAP_FILE)
    client = load_google_client(GOOGLE_CLIENT_JSON_FILE)
    os.environ["GOOGLE_CLIENT_ID"] = client["client_id"]
    os.environ["GOOGLE_CLIENT_SECRET"] = client["client_secret"]
    rendered = render_template(TEMPLATE_PATH.read_text(encoding="utf-8"), build_collections_json(entries))
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config-", dir=OUTPUT_PATH.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        os.replace(temporary_name, OUTPUT_PATH)
    finally:
        pathlib.Path(temporary_name).unlink(missing_ok=True)
    print(f"[render-config] Wrote {OUTPUT_PATH} with {len(entries)} mapped calendars")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[render-config] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
