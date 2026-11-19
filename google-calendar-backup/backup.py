#!/usr/bin/env python3
"""Read-only Google Calendar backup to verified ICS snapshots."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

for shared_path in ("/app/project-settings", str(Path(__file__).resolve().parent.parent / "settings")):
    if shared_path not in sys.path:
        sys.path.insert(0, shared_path)
from google_auth import load_google_client, token_matches_client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backup_storage import new_staging, publish_snapshot

logging.Formatter.converter = time.gmtime
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%SZ")
log = logging.getLogger("google-calendar-backup")

BACKUP_DIR = Path(os.environ.get("GOOGLE_CALENDAR_BACKUP_DIR", "/backup"))
RETENTION = max(1, int(os.environ.get("GOOGLE_CALENDAR_SNAPSHOT_RETENTION", "14")))
INTERVAL = max(60, int(os.environ.get("GOOGLE_CALENDAR_BACKUP_INTERVAL_SECONDS", "14400")))
MAX_AGE = max(60, int(os.environ.get("GOOGLE_CALENDAR_BACKUP_MAX_AGE", "43200")))
ATTEMPTS = max(1, int(os.environ.get("GOOGLE_CALENDAR_BACKUP_ATTEMPTS", "3")))
SCOPE = "https://www.googleapis.com/auth/calendar"


def escape_ical(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def format_datetime(value: str | None) -> tuple[str, str]:
    if not value:
        return "", ""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if "T" not in value:
        return parsed.strftime("%Y%m%d"), ";VALUE=DATE"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), ""


def event_to_ics(event: dict[str, Any]) -> str:
    lines = ["BEGIN:VEVENT", f"UID:{escape_ical(event.get('iCalUID') or event.get('id'))}"]
    for key, field in (("SUMMARY", "summary"), ("DESCRIPTION", "description"), ("LOCATION", "location"),
                       ("STATUS", "status"), ("TRANSP", "transparency")):
        if event.get(field):
            lines.append(f"{key}:{escape_ical(event[field])}")
    for key, field in (("CREATED", "created"), ("LAST-MODIFIED", "updated")):
        if event.get(field):
            value, suffix = format_datetime(event[field])
            if value and not suffix:
                lines.append(f"{key}:{value}")
    start = event.get("start", {})
    end = event.get("end", {})
    start_value, start_suffix = format_datetime(start.get("dateTime") or start.get("date"))
    end_value, end_suffix = format_datetime(end.get("dateTime") or end.get("date"))
    if start_value:
        lines.append(f"DTSTART{start_suffix}:{start_value}")
    if end_value:
        lines.append(f"DTEND{end_suffix}:{end_value}")
    if event.get("sequence") is not None:
        lines.append(f"SEQUENCE:{int(event['sequence'])}")
    if event.get("htmlLink"):
        lines.append(f"URL:{escape_ical(event['htmlLink'])}")
    organizer = event.get("organizer", {})
    if organizer.get("email"):
        lines.append(f"ORGANIZER;CN={escape_ical(organizer.get('displayName'))}:mailto:{organizer['email']}")
    for attendee in event.get("attendees", []):
        if attendee.get("email"):
            params = []
            if attendee.get("displayName"):
                params.append(f"CN={escape_ical(attendee['displayName'])}")
            if attendee.get("responseStatus"):
                params.append(f"PARTSTAT={attendee['responseStatus'].upper()}")
            prefix = ";" + ";".join(params) if params else ""
            lines.append(f"ATTENDEE{prefix}:mailto:{attendee['email']}")
    lines.extend(event.get("recurrence", []))
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def calendar_to_ics(calendar: dict[str, Any], events: list[dict[str, Any]]) -> str:
    name = escape_ical(calendar.get("summary") or calendar.get("id"))
    components = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//syncer//Google Calendar Backup//EN",
        "CALSCALE:GREGORIAN", f"X-WR-CALNAME:{name}",
    ]
    components.extend(event_to_ics(event) for event in events)
    components.append("END:VCALENDAR")
    return "\r\n".join(components) + "\r\n"


def sanitize_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._") or "unnamed"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_snapshot(calendars: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> dict[str, Any]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    staging = new_staging(BACKUP_DIR)
    try:
        used_names: set[str] = set()
        entries = []
        for calendar, events in calendars:
            base = sanitize_filename(calendar.get("summary") or calendar.get("id", "calendar"))
            filename = f"calendar_{base}.ics"
            if filename in used_names:
                filename = f"calendar_{base}_{sanitize_filename(calendar.get('id', ''))}.ics"
            used_names.add(filename)
            (staging / filename).write_text(calendar_to_ics(calendar, events), encoding="utf-8", newline="")
            entries.append({"id": calendar.get("id"), "name": calendar.get("summary"), "file": filename,
                            "events": len(events)})
        if not entries:
            raise RuntimeError("Google returned no calendars")
        checksums = {path.name: sha256(path) for path in staging.glob("*.ics")}
        manifest = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "calendars": entries,
            "stats": {"calendars": len(entries), "events": sum(entry["events"] for entry in entries)},
            "checksums": checksums,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        verify_snapshot(staging)
        (staging / "COMPLETE").write_text(manifest["timestamp"] + "\n", encoding="utf-8")
        publish_snapshot(staging, BACKUP_DIR, RETENTION)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def verify_snapshot(snapshot: Path) -> dict[str, Any]:
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    checksums = manifest.get("checksums", {})
    actual = {path.name for path in snapshot.glob("*.ics")}
    if not checksums or actual != set(checksums):
        raise RuntimeError("Snapshot file inventory does not match manifest")
    for name, expected in checksums.items():
        if Path(name).name != name or sha256(snapshot / name) != expected:
            raise RuntimeError("Snapshot checksum mismatch")
    return manifest


def check_health() -> None:
    manifest = verify_snapshot(BACKUP_DIR / "current")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(manifest["timestamp"])).total_seconds()
    if age < -300 or age > MAX_AGE:
        raise RuntimeError("Last successful backup is too old")
    status_path = BACKUP_DIR / "status.json"
    if status_path.exists() and json.loads(status_path.read_text(encoding="utf-8")).get("result") == "failed":
        raise RuntimeError("Most recent backup attempt failed")
    log.info("Verified current backup: checksums OK, age %.1f hours", age / 3600)


class GoogleCalendarClient:
    def __init__(self) -> None:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_path = Path(os.environ["GOOGLE_CALENDAR_TOKEN_FILE"])
        payload = json.loads(token_path.read_text(encoding="utf-8"))
        client = load_google_client(Path(os.environ["GOOGLE_CLIENT_JSON_FILE"]))
        if not token_matches_client(payload, client, ("access_token", "refresh_token"), SCOPE,
                                    token_path.with_name("google-client-id").read_text().strip()):
            raise RuntimeError("Google Calendar token does not match the configured Desktop client")
        credentials = Credentials(
            token=payload.get("access_token") or payload.get("token"),
            refresh_token=payload["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client["client_id"],
            client_secret=client["client_secret"],
            scopes=[SCOPE],
        )
        if not credentials.valid and credentials.refresh_token:
            credentials.refresh(Request())
        self.service = build("calendar", "v3", credentials=credentials)

    def fetch(self) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        calendars = []
        page_token = None
        while True:
            response = self.service.calendarList().list(pageToken=page_token, showDeleted=False).execute()
            calendars.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        result = []
        for calendar in calendars:
            events = []
            page_token = None
            while True:
                response = self.service.events().list(calendarId=calendar["id"], pageToken=page_token,
                                                      showDeleted=False, singleEvents=False,
                                                      maxResults=2500).execute()
                events.extend(response.get("items", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            result.append((calendar, events))
        return result


def write_status(result: str, error: str | None = None) -> None:
    payload: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }
    if error:
        payload["error"] = error[:240]
    (BACKUP_DIR / "status.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")


def run_backup() -> dict[str, Any]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with (BACKUP_DIR / ".backup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another backup is already running") from exc
        last_error: Exception | None = None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                manifest = write_snapshot(GoogleCalendarClient().fetch())
                write_status("success")
                break
            except Exception as exc:
                last_error = exc
                log.error("Attempt %d/%d failed: %s", attempt, ATTEMPTS, exc)
                if attempt < ATTEMPTS:
                    time.sleep(min(60, 5 * attempt))
        else:
            write_status("failed", str(last_error))
            assert last_error is not None
            raise last_error
    log.info("Backup complete: %d calendars, %d events", manifest["stats"]["calendars"], manifest["stats"]["events"])
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    if args.health:
        check_health()
        return 0
    if not args.watch:
        run_backup()
        return 0
    while True:
        try:
            run_backup()
        except Exception as exc:
            log.error("Backup failed: %s", exc)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
