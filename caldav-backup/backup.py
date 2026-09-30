#!/usr/bin/env python3
"""
caldav-backup — backup completo del server CalDAV in formato ICS

Esporta tutti i calendari (VEVENT) e le liste task (VTODO) dal server CalDAV
in file ICS separati, con snapshot periodici e promozione dell'ultimo backup.

Usage:
    python backup.py                    # backup singolo
    python backup.py --watch            # backup periodico
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import caldav

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


# CalDAV credentials from environment variables
CALDAV_URL = require_env("CALDAV_URL")
CALDAV_USERNAME = require_env("CALDAV_USERNAME")
CALDAV_PASSWORD = require_env("CALDAV_PASSWORD")


# Optional: backup directory (default: ./caldav-backup-output)
BACKUP_DIR = Path(os.environ.get("CALDAV_BACKUP_DIR", "./caldav-backup-output"))
SNAPSHOT_RETENTION = max(1, int(os.environ.get("CALDAV_BACKUP_RETENTION", "14")))


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

        todo_error = event_error = None
        try:
            todos = cal.todos(include_completed=True)
            if todos or todo_type:
                todo_lists.append({
                    "name": display_name,
                    "url": cal_url,
                    "items": todos,
                    "count": len(todos),
                })
                log.info("[Discover] VTODO: '%s' (%d items)", display_name, len(todos))
        except Exception as exc:
            todo_error = exc
            log.debug("[Discover] No todos in %s: %s", display_name, exc)

        try:
            events = cal.events()
            if events or todo_type is False:
                calendars.append({
                    "name": display_name,
                    "url": cal_url,
                    "items": events,
                    "count": len(events),
                })
                log.info("[Discover] Calendar: '%s' (%d events)", display_name, len(events))
        except Exception as exc:
            event_error = exc
            log.debug("[Discover] No events in %s: %s", display_name, exc)

        if (todo_type is True and todo_error) or (todo_type is False and event_error) or (todo_error and event_error):
            raise RuntimeError(f"Could not read collection {display_name}") from (todo_error or event_error)

    return calendars, todo_lists


def export_calendar(events: list, name: str, backup_path: Path) -> int:
    """Export a calendar (VEVENT) to ICS file."""
    ics_path = backup_path / f"calendar_{sanitize_filename(name)}.ics"

    try:
        ics_content = build_ics_from_vevents(events)

        ics_path.write_text(ics_content, encoding="utf-8")
        log.info("[Export] Calendar '%s' -> %s (%d events)", name, ics_path.name, len(events or []))
        return len(events)

    except Exception as exc:
        log.error("[Export] Error exporting calendar '%s': %s", name, exc)
        raise


def export_todo_list(todos: list, name: str, backup_path: Path) -> int:
    """Export a VTODO list to ICS file."""
    ics_path = backup_path / f"tasks_{sanitize_filename(name)}.ics"

    try:
        ics_content = build_ics_from_vtodos(todos)

        ics_path.write_text(ics_content, encoding="utf-8")
        log.info("[Export] Tasks '%s' -> %s (%d items)", name, ics_path.name, len(todos or []))
        return len(todos)

    except Exception as exc:
        log.error("[Export] Error exporting todo list '%s': %s", name, exc)
        raise


def sanitize_filename(name: str) -> str:
    """Sanitize string for use in filename."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip()
    return name or "unnamed"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prune_snapshots(backup_root: Path, retention: int = SNAPSHOT_RETENTION) -> None:
    snapshots_dir = backup_root / "snapshots"
    snapshots = sorted(
        (path for path in snapshots_dir.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    ) if snapshots_dir.exists() else []
    for snapshot in snapshots[retention:]:
        shutil.rmtree(snapshot)


def promote_snapshot(staging: Path, backup_root: Path, metadata: dict[str, Any]) -> Path:
    files = sorted(path for path in staging.rglob("*.ics") if path.is_file())
    if not files:
        raise RuntimeError("Backup produced no ICS files")

    latest = backup_root / "latest"
    if latest.is_dir():
        previous_files = {path.name for path in latest.glob("*.ics")}
        current_files = {path.name for path in files}
        missing = previous_files - current_files
        if missing:
            raise RuntimeError("Backup is missing previous collections: " + ", ".join(sorted(missing)))

    checksums = {
        str(path.relative_to(staging)): sha256_file(path)
        for path in files
    }
    manifest = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stats": metadata.get("stats", {}),
        "calendars": metadata.get("calendars", []),
        "todo_lists": metadata.get("todo_lists", []),
        "checksums": checksums,
    }
    (staging / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )

    snapshots_dir = backup_root / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    snapshot = snapshots_dir / datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    suffix = 1
    while snapshot.exists():
        snapshot = snapshots_dir / f"{snapshot.name}-{suffix}"
        suffix += 1
    os.replace(staging, snapshot)

    latest_staging = backup_root / ".latest-staging"
    previous = backup_root / ".latest-previous"
    try:
        if latest_staging.exists():
            shutil.rmtree(latest_staging)
        shutil.copytree(snapshot, latest_staging)
        if previous.exists():
            shutil.rmtree(previous)
        if latest.exists():
            os.replace(latest, previous)
        os.replace(latest_staging, latest)
    except Exception:
        if not latest.exists() and previous.exists():
            os.replace(previous, latest)
        raise
    finally:
        if latest_staging.exists():
            shutil.rmtree(latest_staging)
        if previous.exists():
            shutil.rmtree(previous)

    prune_snapshots(backup_root)
    return latest


def build_ics(items: list, component: str) -> str:
    """Keep every item component and its timezone definitions in one calendar."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//caldav-backup//EN"]
    if component == "VEVENT":
        lines.append("CALSCALE:GREGORIAN")
    timezones: list[str] = []
    entries: list[str] = []
    for item in items:
        data = item.data
        found = re.findall(f"BEGIN:{component}.*?END:{component}", data, flags=re.DOTALL)
        if not found:
            raise RuntimeError(f"CalDAV returned an item without a complete {component} component")
        entries.extend(found)
        for timezone_block in re.findall(r"BEGIN:VTIMEZONE.*?END:VTIMEZONE", data, flags=re.DOTALL):
            if timezone_block not in timezones:
                timezones.append(timezone_block)
    return "\n".join([*lines, *timezones, *entries, "END:VCALENDAR", ""])


def build_ics_from_vevents(events: list) -> str:
    return build_ics(events, "VEVENT")


def build_ics_from_vtodos(todos: list) -> str:
    return build_ics(todos, "VTODO")


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

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=BACKUP_DIR))

    stats = {"calendars": 0, "events": 0, "todo_lists": 0, "todos": 0}
    failures = []

    # Export calendars (VEVENT)
    log.info("-" * 40)
    log.info("Exporting calendars (VEVENT)...")
    for cal_info in calendars:
        try:
            count = export_calendar(cal_info["items"], cal_info["name"], staging)
            stats["calendars"] += 1
            stats["events"] += count
        except Exception as exc:
            log.error("Error exporting calendar %s: %s", cal_info["name"], exc)
            failures.append(f"calendar:{cal_info['name']}: {exc}")

    # Export todo lists (VTODO)
    log.info("-" * 40)
    log.info("Exporting task lists (VTODO)...")
    for todo_info in todo_lists:
        try:
            count = export_todo_list(todo_info["items"], todo_info["name"], staging)
            stats["todo_lists"] += 1
            stats["todos"] += count
        except Exception as exc:
            log.error("Error exporting task list %s: %s", todo_info["name"], exc)
            failures.append(f"todo:{todo_info['name']}: {exc}")

    if failures:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError("Backup failed: " + "; ".join(failures))

    metadata = {
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
    try:
        latest = promote_snapshot(staging, BACKUP_DIR, metadata)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    log.info("=" * 60)
    log.info("Backup complete!")
    log.info("  Calendars: %d (%d events)", stats["calendars"], stats["events"])
    log.info("  Task lists: %d (%d items)", stats["todo_lists"], stats["todos"])
    log.info("  Output: %s", latest)
    log.info("=" * 60)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="CalDAV backup tool")
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

    if args.watch:
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
