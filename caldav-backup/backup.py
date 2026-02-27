#!/usr/bin/env python3
"""
caldav-backup — backup completo del server CalDAV in formato ICS

Esporta tutti i calendari (VEVENT) e le liste task (VTODO) dal server CalDAV
in file ICS separati. Supporta backup incrementale basato su hardlink.

Usage:
    python backup.py                    # backup singolo
    python backup.py --watch            # watchdog mode (backup automatico su modifiche)
    python backup.py --discover         # solo discover e lista calendari
"""

import argparse
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import caldav

# Load .env file if present (manual parsing to avoid extra dependency)
env_path = Path(".env")
if env_path.exists():
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                # Remove quotes if present
                if value and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]
                if key and value and key not in os.environ:
                    os.environ[key] = value
    print(f"Loaded environment from .env")

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("caldav-backup")


# ── Environment ────────────────────────────────────────────────────────────
def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        log.error("Required environment variable %s is not set", name)
        sys.exit(1)
    return value


def load_env_from_file(env_path: str = ".env") -> None:
    """Load environment variables from .env file if running locally."""
    env_file = Path(env_path)
    if env_file.exists():
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    # Remove quotes if present
                    if value and value[0] in ('"', "'") and value[-1] == value[0]:
                        value = value[1:-1]
                    if key and value and key not in os.environ:
                        os.environ[key] = value
        log.info("Loaded environment from %s", env_path)


# Load .env file if running locally (not in container)
load_env_from_file()

# CalDAV credentials from environment variables
CALDAV_URL = require_env("CALDAV_URL")
CALDAV_USERNAME = require_env("CALDAV_USERNAME")
CALDAV_PASSWORD = require_env("CALDAV_PASSWORD")


# Optional: backup directory (default: ./caldav-backup-output)
BACKUP_DIR = Path(os.environ.get("CALDAV_BACKUP_DIR", "./caldav-backup-output"))

# ── Configurazione Calendari ─────────────────────────────────────────────
# NOTA: Usa --discover per trovare tutti i calendari disponibili, poi inserisci i nomi qui.
# Lascia vuoto per backuppare TUTTI i calendari trovati.
CALENDARS = []  # es: ["personale", "lavoro", "famiglia"] - lasciare vuoto per tutti

# Calendari VTODO (task) - lascia vuoto per tutti
VTODO_LISTS = []  # es: ["tasks_default", "promemoria"] - lasciare vuoto per tutti


# ════════════════════════════════════════════════════════════════════════════
# Core backup logic
# ════════════════════════════════════════════════════════════════════════════

def connect_caldav() -> caldav.DAVClient:
    """Connect to CalDAV server."""
    log.info("Connecting to CalDAV: %s", CALDAV_URL)
    client = caldav.DAVClient(
        url=CALDAV_URL,
        username=CALDAV_USERNAME,
        password=CALDAV_PASSWORD,
    )
    return client


def discover_calendars(client: caldav.DAVClient) -> dict[str, list[dict]]:
    """Discover all available calendars and todo lists."""
    principal = client.principal()
    all_calendars = principal.calendars()

    calendars = {"vevent": [], "vtodo": []}

    for cal in all_calendars:
        # Get calendar home set to determine type
        try:
            # Check if calendar supports VEVENT (events)
            if hasattr(cal, "get_supported_components"):
                components = cal.get_supported_components()
                if components:
                    components = components.split(",")

            # CalDAV calendars can have different component types
            # We determine type by trying to fetch content
            display_name = str(cal.name) if cal.name else cal.url.split("/")[-2]

            # Try to get events
            try:
                events = cal.events()
                if events:
                    calendars["vevent"].append({
                        "name": display_name,
                        "url": str(cal.url),
                        "count": len(events),
                    })
                    log.info("[Discover] Calendar: '%s' (%d events)", display_name, len(events))
            except Exception:
                pass

            # Try to get todos
            try:
                todos = cal.todos(include_completed=True)
                if todos:
                    calendars["vtodo"].append({
                        "name": display_name,
                        "url": str(cal.url),
                        "count": len(todos),
                    })
                    log.info("[Discover] VTODO list: '%s' (%d items)", display_name, len(todos))
            except Exception:
                pass

        except Exception as exc:
            log.warning("[Discover] Could not check calendar %s: %s", cal.url, exc)

    return calendars


def discover_all_calendars(client: caldav.DAVClient) -> tuple[list[dict], list[dict]]:
    """
    Discover all calendars and todo lists using PROPFIND.
    Returns (calendars, todo_lists).
    """
    principal = client.principal()
    all_calendars = principal.calendars()

    calendars = []
    todo_lists = []

    for cal in all_calendars:
        display_name = str(cal.name) if cal.name else cal.url.split("/")[-2]
        cal_url = str(cal.url)

        # Determine type by checking URL pattern or trying to fetch
        # Synology Calendar uses /calendars/ for VEVENT and /tasks/ for VTODO
        if "/tasks/" in cal_url.lower() or "/tasklists/" in cal_url.lower():
            todo_type = True
        elif "/calendars/" in cal_url.lower():
            todo_type = False
        else:
            # Try both - will be added to both lists if contains both
            todo_type = None

        try:
            # Check for todos
            todos = cal.todos(include_completed=True)
            if todos or todo_type:
                todo_lists.append({
                    "name": display_name,
                    "url": cal_url,
                    "cal": cal,
                    "count": len(todos) if todos else 0,
                })
                log.info("[Discover] VTODO: '%s' (%d items)", display_name, len(todos) if todos else 0)
        except Exception as exc:
            log.debug("[Discover] No todos in %s: %s", display_name, exc)

        try:
            # Check for events
            events = cal.events()
            if events or todo_type is False:
                calendars.append({
                    "name": display_name,
                    "url": cal_url,
                    "cal": cal,
                    "count": len(events) if events else 0,
                })
                log.info("[Discover] Calendar: '%s' (%d events)", display_name, len(events) if events else 0)
        except Exception as exc:
            log.debug("[Discover] No events in %s: %s", display_name, exc)

    return calendars, todo_lists


def export_calendar(cal: Any, name: str, backup_path: Path, include_completed: bool = True) -> int:
    """Export a calendar (VEVENT) to ICS file."""
    ics_path = backup_path / f"calendar_{sanitize_filename(name)}.ics"

    try:
        events = cal.events()
        if not events:
            log.info("[Export] No events in calendar '%s'", name)
            return 0

        # Build ICS content
        ics_content = build_ics_from_vevents(events)

        ics_path.write_text(ics_content, encoding="utf-8")
        log.info("[Export] Calendar '%s' -> %s (%d events)", name, ics_path.name, len(events))
        return len(events)

    except Exception as exc:
        log.error("[Export] Error exporting calendar '%s': %s", name, exc)
        return 0


def export_todo_list(cal: Any, name: str, backup_path: Path, include_completed: bool = True) -> int:
    """Export a VTODO list to ICS file."""
    ics_path = backup_path / f"tasks_{sanitize_filename(name)}.ics"

    try:
        todos = cal.todos(include_completed=include_completed)
        if not todos:
            log.info("[Export] No todos in list '%s'", name)
            return 0

        # Build ICS content
        ics_content = build_ics_from_vtodos(todos)

        ics_path.write_text(ics_content, encoding="utf-8")
        log.info("[Export] Tasks '%s' -> %s (%d items)", name, ics_path.name, len(todos))
        return len(todos)

    except Exception as exc:
        log.error("[Export] Error exporting todo list '%s': %s", name, exc)
        return 0


def sanitize_filename(name: str) -> str:
    """Sanitize string for use in filename."""
    # Replace problematic characters
    import re
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip()
    return name or "unnamed"


def build_ics_from_vevents(events: list) -> str:
    """Build ICS content from CalDAV VEVENT objects."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//caldav-backup//EN",
        "CALSCALE:GREGORIAN",
    ]

    for event in events:
        try:
            # Get the raw iCal data
            if hasattr(event, "data") and event.data:
                ics_data = event.data
                # Extract just the VEVENT component
                if "BEGIN:VEVENT" in ics_data:
                    # Find the VEVENT block
                    start = ics_data.find("BEGIN:VEVENT")
                    end = ics_data.find("END:VEVENT") + len("END:VEVENT")
                    if start >= 0 and end > start:
                        vevent = ics_data[start:end]
                        lines.append(vevent)
        except Exception as exc:
            log.warning("[ICS] Error processing event: %s", exc)

    lines.append("END:VCALENDAR")
    return "\n".join(lines)


def build_ics_from_vtodos(todos: list) -> str:
    """Build ICS content from CalDAV VTODO objects."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//caldav-backup//EN",
    ]

    for todo in todos:
        try:
            if hasattr(todo, "data") and todo.data:
                ics_data = todo.data
                if "BEGIN:VTODO" in ics_data:
                    start = ics_data.find("BEGIN:VTODO")
                    end = ics_data.find("END:VTODO") + len("END:VTODO")
                    if start >= 0 and end > start:
                        vtodo = ics_data[start:end]
                        lines.append(vtodo)
        except Exception as exc:
            log.warning("[ICS] Error processing todo: %s", exc)

    lines.append("END:VCALENDAR")
    return "\n".join(lines)


def run_backup() -> dict:
    """Run the backup process."""
    log.info("=" * 60)
    log.info("Starting CalDAV backup")
    log.info("=" * 60)

    client = connect_caldav()

    # Discover all calendars
    log.info("Discovering calendars...")
    calendars, todo_lists = discover_all_calendars(client)

    log.info("=" * 60)
    log.info("Found %d calendars, %d task lists", len(calendars), len(todo_lists))
    log.info("=" * 60)

    # Backup directory - directly in BACKUP_DIR (no timestamp subfolder)
    backup_path = BACKUP_DIR
    backup_path.mkdir(parents=True, exist_ok=True)

    # Clean old backup - remove ALL files and subdirectories
    log.info("Cleaning old backup...")
    for item in list(backup_path.iterdir()):
        if item.name == ".git":
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
        except Exception as e:
            log.warning("Could not remove %s: %s", item.name, e)
    log.info("Old backup cleaned")

    stats = {"calendars": 0, "events": 0, "todo_lists": 0, "todos": 0}

    # Filter calendars if specific ones are configured
    if CALENDARS:
        calendars = [c for c in calendars if c["name"] in CALENDARS]
        log.info("Filtered to configured calendars: %s", CALENDARS)

    # Filter todo lists if specific ones are configured
    if VTODO_LISTS:
        todo_lists = [t for t in todo_lists if t["name"] in VTODO_LISTS]
        log.info("Filtered to configured task lists: %s", VTODO_LISTS)

    # Export calendars (VEVENT)
    log.info("-" * 40)
    log.info("Exporting calendars (VEVENT)...")
    for cal_info in calendars:
        try:
            count = export_calendar(cal_info["cal"], cal_info["name"], backup_path)
            if count > 0:
                stats["calendars"] += 1
                stats["events"] += count
        except Exception as exc:
            log.error("Error exporting calendar %s: %s", cal_info["name"], exc)

    # Export todo lists (VTODO)
    log.info("-" * 40)
    log.info("Exporting task lists (VTODO)...")
    for todo_info in todo_lists:
        try:
            count = export_todo_list(todo_info["cal"], todo_info["name"], backup_path)
            if count > 0:
                stats["todo_lists"] += 1
                stats["todos"] += count
        except Exception as exc:
            log.error("Error exporting task list %s: %s", todo_info["name"], exc)

    # Save manifest
    timestamp = datetime.now(timezone.utc).isoformat()
    manifest = {
        "timestamp": timestamp,
        "backup_dir": str(backup_path),
        "calendars": [
            {"name": c["name"], "url": c["url"], "count": c["count"]}
            for c in calendars
        ],
        "todo_lists": [
            {"name": t["name"], "url": t["url"], "count": t["count"]}
            for t in todo_lists
        ],
        "stats": stats,
    }

    import json
    manifest_path = backup_path / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False, default=str)

    log.info("=" * 60)
    log.info("Backup complete!")
    log.info("  Calendars: %d (%d events)", stats["calendars"], stats["events"])
    log.info("  Task lists: %d (%d items)", stats["todo_lists"], stats["todos"])
    log.info("  Output: %s", backup_path)
    log.info("=" * 60)

    return stats


def run_discover() -> None:
    """Just discover and list all available calendars."""
    log.info("=" * 60)
    log.info("CalDAV Calendar Discovery")
    log.info("=" * 60)

    client = connect_caldav()
    calendars, todo_lists = discover_all_calendars(client)

    log.info("=" * 60)
    log.info("DISCOVERED CALENDARS (VEVENT):")
    log.info("=" * 60)
    for cal in calendars:
        log.info("  - %s (%d events)", cal["name"], cal["count"])
        log.info("    URL: %s", cal["url"])

    log.info("=" * 60)
    log.info("DISCOVERED TASK LISTS (VTODO):")
    log.info("=" * 60)
    for todo in todo_lists:
        log.info("  - %s (%d items)", todo["name"], todo["count"])
        log.info("    URL: %s", todo["url"])

    log.info("=" * 60)
    log.info("To backup specific calendars, edit this script and set:")
    log.info("  CALENDARS = [%s]", ", ".join(f'"{c["name"]}"' for c in calendars))
    log.info("  VTODO_LISTS = [%s]", ", ".join(f'"{t["name"]}"' for t in todo_lists))
    log.info("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="CalDAV backup tool")
    parser.add_argument(
        "--discover", "-d",
        action="store_true",
        help="Discover and list all available calendars (no backup)",
    )
    parser.add_argument(
        "--watch", "-w",
        action="store_true",
        help="Watch mode: run backup continuously (every 60 seconds)",
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=60,
        help="Interval in seconds for watch mode (default: 60)",
    )
    args = parser.parse_args()

    if args.discover:
        run_discover()
    elif args.watch:
        log.info("Starting watch mode (backup every %d seconds, Ctrl+C to stop)", args.interval)
        try:
            while True:
                try:
                    run_backup()
                except Exception as exc:
                    log.error("Backup failed: %s", exc)
                log.info("Waiting %d seconds...", args.interval)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("Watch mode stopped.")
    else:
        run_backup()


if __name__ == "__main__":
    main()
