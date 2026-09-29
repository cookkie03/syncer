#!/usr/bin/env python3
"""Refresh calendar pairing IDs by matching discover output on calendar names."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys


CALENDAR_MAP_FILE = pathlib.Path("/app/calendar-map.json")
DISCOVER_LINE = re.compile(r'^\s*-\s+"(?P<id>[^"]+)"\s+\("(?P<name>.*)"\)$')


def parse_discover_output(output: str) -> tuple[dict[str, str], dict[str, str]]:
    current = None
    caldav: dict[str, str] = {}
    google: dict[str, str] = {}

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if line == "caldav_calendars:":
            current = caldav
            continue
        if line == "google_calendars:":
            current = google
            continue
        match = DISCOVER_LINE.match(line)
        if current is not None and match:
            name = match.group("name")
            if name in current and current[name] != match.group("id"):
                raise RuntimeError(f"Ambiguous calendar name in discover: {name}")
            current[name] = match.group("id")
    return caldav, google


def run_discover() -> str:
    result = subprocess.run(
        ["vdirsyncer", "discover", "caldav_gcal"],
        check=False,
        capture_output=True,
        text=True,
        input="y\n" * 100,
    )
    output = result.stdout
    if result.returncode == 0 and "caldav_calendars:" in output and "google_calendars:" in output:
        return output
    raise RuntimeError(output + ("\n" if output else "") + result.stderr)


def load_map(path: pathlib.Path) -> list[dict[str, str]]:
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_map(entries: list[dict[str, str]], caldav: dict[str, str], google: dict[str, str]) -> tuple[list[dict[str, str]], list[str]]:
    updated: list[dict[str, str]] = []
    warnings: list[str] = []

    missing = []
    for entry in entries:
        name = entry["name"]
        caldav_id = caldav.get(name)
        google_id = google.get(name)
        if not caldav_id:
            missing.append(f"CalDAV: {name}")
        if not google_id:
            missing.append(f"Google: {name}")
        if not caldav_id or not google_id:
            continue
        updated.append({"name": name, "caldav": caldav_id, "google": google_id})

    if missing:
        raise RuntimeError("Pairing incomplete; sync stopped. Missing " + ", ".join(missing))

    known_names = {entry["name"] for entry in entries}
    for name in sorted((set(caldav) & set(google)) - known_names):
        updated.append({"name": name, "caldav": caldav[name], "google": google[name]})
        warnings.append(f"Added new matching calendar: {name}")

    return updated, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh vdirsyncer calendar pairing file")
    parser.add_argument("--write", action="store_true", help="Write refreshed IDs back to the calendar map file")
    parser.add_argument("--map-file", default=str(CALENDAR_MAP_FILE), help="Calendar map JSON file")
    args = parser.parse_args()

    map_file = pathlib.Path(args.map_file)
    entries = load_map(map_file)
    output = run_discover()
    caldav, google = parse_discover_output(output)
    refreshed, warnings = refresh_map(entries, caldav, google)

    print(json.dumps(refreshed, indent=2, ensure_ascii=False))
    if warnings:
        print("\nWarnings:", file=sys.stderr)
        for warning in warnings:
            print(f"- {warning}", file=sys.stderr)

    if args.write:
        temporary = map_file.with_name(map_file.name + ".tmp")
        temporary.write_text(json.dumps(refreshed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(map_file)
        print(f"[refresh-pairing] Updated {map_file}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[refresh-pairing] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
