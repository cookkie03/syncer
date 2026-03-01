#!/usr/bin/env python3
"""
vtodo-notion — Bidirectional CalDAV VTODO <-> Notion sync.
Architecture: Snapshot & Reconcile.
"""

import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import caldav
import requests
import vobject
from dateutil.rrule import rrulestr
from notion_client import Client
from notion_client.errors import APIResponseError

# ── Config ────────────────────────────────────────────────────────────────

CALDAV_URL = os.environ["CALDAV_URL"]
CALDAV_USERNAME = os.environ["CALDAV_USERNAME"]
CALDAV_PASSWORD = os.environ["CALDAV_PASSWORD"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/sync_state.json"))
LOG_DIR = Path(os.environ.get("LOG_DIR", "/data/logs"))
LOG_FILE = LOG_DIR / "sync.log"

MAX_RETRIES = 3
CIRCUIT_BREAKER_THRESHOLD = 5
RECURRING_CLEANUP_DAYS = 10

# ── Logging ───────────────────────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)

file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"))

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))

log = logging.getLogger("sync")
log.setLevel(logging.DEBUG)
log.addHandler(file_handler)
log.addHandler(stdout_handler)


# ── Data Model ────────────────────────────────────────────────────────────

@dataclass
class TaskData:
    uid: str
    summary: str = ""
    description: str = ""
    due: str | None = None          # YYYY-MM-DD
    priority: str = "Nessuna"       # Alta/Media/Bassa/Nessuna
    status: str = "In corso"        # In corso/Completato
    is_completed: bool = False
    location: str = ""
    url: str = ""
    rrule: str = ""
    list_name: str = ""
    last_modified: str = ""         # ISO timestamp for conflict resolution
    notion_page_id: str | None = None   # Notion-only

    def content_hash(self) -> str:
        """Hash of semantic fields only (no timestamps, no page IDs)."""
        fields = (
            self.summary.strip(),
            self.description.strip()[:1990],
            (self.due or "")[:10],
            self.priority,
            str(self.is_completed),
            self.location.strip(),
            self.url.strip(),
            self.rrule.strip(),
            self.list_name.strip(),
        )
        return hashlib.sha256("|".join(fields).encode()).hexdigest()[:16]


@dataclass
class SyncState:
    known_uids: dict[str, str] = field(default_factory=dict)  # uid -> content_hash
    last_sync: str | None = None


def load_state() -> SyncState:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            # Auto-migrate from old format (caldav_modified/notion_modified keys)
            if "caldav_modified" in data and "known_uids" not in data:
                log.info("[State] Migrating from old format to new known_uids format")
                return SyncState(known_uids={}, last_sync=data.get("last_sync"))
            return SyncState(
                known_uids=data.get("known_uids", {}),
                last_sync=data.get("last_sync"),
            )
        except Exception as e:
            log.warning("[State] Could not load state: %s — starting fresh", e)
    return SyncState()


def save_state(state: SyncState) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(asdict(state), indent=2))


# ── Notifications ─────────────────────────────────────────────────────────

def notify(title: str, message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": f"*{title}*\n{message}", "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.warning("[Telegram] Failed to send: %s", e)


# ── CalDAV Layer ──────────────────────────────────────────────────────────

PRIORITY_MAP = {0: "Nessuna", 1: "Alta", 2: "Alta", 3: "Media", 4: "Media", 5: "Media", 6: "Bassa", 7: "Bassa", 8: "Bassa", 9: "Bassa"}
PRIORITY_REVERSE = {"Alta": "1", "Media": "5", "Bassa": "9", "Nessuna": "0"}
STATUS_MAP = {"COMPLETED": "Completato", "IN-PROCESS": "In corso", "NEEDS-ACTION": "In corso"}


def _ical_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n").replace(";", "\\;")


def parse_vtodo(vtodo_comp, list_name: str) -> TaskData:
    """Parse a vobject VTODO component into a TaskData."""
    uid = str(vtodo_comp.uid.value) if hasattr(vtodo_comp, "uid") else ""
    summary = str(vtodo_comp.summary.value) if hasattr(vtodo_comp, "summary") else ""
    description = str(vtodo_comp.description.value) if hasattr(vtodo_comp, "description") else ""

    due = None
    if hasattr(vtodo_comp, "due") and vtodo_comp.due.value:
        dt_obj = vtodo_comp.due.value
        if hasattr(dt_obj, "dt"):
            dt_obj = dt_obj.dt
        if isinstance(dt_obj, (datetime, date)):
            due = dt_obj.isoformat()[:10]  # YYYY-MM-DD only

    priority_raw = vtodo_comp.priority.value if hasattr(vtodo_comp, "priority") else 0
    try:
        priority = PRIORITY_MAP.get(int(priority_raw), "Nessuna")
    except (ValueError, TypeError):
        priority = "Nessuna"

    status_raw = str(vtodo_comp.status.value).upper() if hasattr(vtodo_comp, "status") else ""
    status = STATUS_MAP.get(status_raw, "In corso")
    is_completed = status == "Completato"

    rrule = str(vtodo_comp.rrule.value) if hasattr(vtodo_comp, "rrule") else ""
    location = str(vtodo_comp.location.value) if hasattr(vtodo_comp, "location") else ""
    url_val = str(vtodo_comp.url.value) if hasattr(vtodo_comp, "url") else ""

    # Extract last-modified timestamp
    last_modified = ""
    for attr in ("last_modified", "dtstamp"):
        if hasattr(vtodo_comp, attr):
            val = getattr(vtodo_comp, attr).value
            if isinstance(val, (datetime, date)):
                last_modified = val.isoformat()
                break
    if not last_modified:
        last_modified = datetime.now(timezone.utc).isoformat()

    return TaskData(
        uid=uid, summary=summary, description=description, due=due,
        priority=priority, status=status, is_completed=is_completed,
        location=location, url=url_val, rrule=rrule,
        list_name=list_name, last_modified=last_modified,
    )


def fetch_caldav_snapshot(client: caldav.DAVClient) -> dict[str, TaskData]:
    """Fetch all VTODOs from CalDAV, deduplicate by UID (prefer active over completed)."""
    snapshot: dict[str, TaskData] = {}
    principal = client.principal()
    calendars = principal.calendars()
    log.info("[CalDAV] Found %d collections", len(calendars))

    for cal in calendars:
        name = str(cal.name) if cal.name else "?"
        try:
            todos = cal.todos(include_completed=True)
        except Exception as e:
            log.warning("[CalDAV] Could not read '%s': %s", name, e)
            continue

        if not todos:
            continue

        log.info("[CalDAV] '%s': %d items", name, len(todos))
        for todo in todos:
            try:
                vtodo_comp = todo.vobject_instance.vtodo
                task = parse_vtodo(vtodo_comp, name)
                if not task.uid:
                    continue

                # Dedup: prefer active (NEEDS-ACTION) over completed instances
                existing = snapshot.get(task.uid)
                if existing:
                    if existing.is_completed and not task.is_completed:
                        log.info("[CalDAV] Duplicate UID %s: prefer active from '%s'", task.uid[:30], name)
                        snapshot[task.uid] = task
                    # else: keep existing (either both active, or existing is already active)
                else:
                    snapshot[task.uid] = task
            except Exception as e:
                log.warning("[CalDAV] Parse error in '%s': %s", name, e)

    log.info("[CalDAV] Snapshot: %d unique UIDs", len(snapshot))
    return snapshot


def build_ical(task: TaskData) -> str:
    """Build iCalendar VTODO string from TaskData."""
    summary = _ical_escape(task.summary or "(senza titolo)")
    desc = _ical_escape(task.description or "")
    priority = PRIORITY_REVERSE.get(task.priority, "0")
    status = "COMPLETED" if task.is_completed else "NEEDS-ACTION"
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//vtodo-notion//EN",
        "BEGIN:VTODO",
        f"UID:{task.uid}", f"DTSTAMP:{now}", f"LAST-MODIFIED:{now}",
        f"SUMMARY:{summary}", f"STATUS:{status}", f"PRIORITY:{priority}",
    ]
    if desc:
        lines.append(f"DESCRIPTION:{desc}")
    if task.due:
        lines.append(f"DUE:{task.due[:10].replace('-', '')}")
    if task.location:
        lines.append(f"LOCATION:{task.location}")
    if task.url:
        lines.append(f"URL:{task.url}")
    if task.rrule:
        lines.append(f"RRULE:{task.rrule}")
    lines.extend(["END:VTODO", "END:VCALENDAR"])
    return "\n".join(lines)


def write_caldav(calendars: list, task: TaskData) -> bool:
    """Create or update a VTODO on CalDAV. Returns True on success."""
    target_cal = None
    for cal in calendars:
        if (str(cal.name) if cal.name else "?") == task.list_name:
            target_cal = cal
            break
    if not target_cal:
        log.warning("[CalDAV] Calendar '%s' not found, skipping write for %s", task.list_name, task.uid[:30])
        return False

    ical = build_ical(task)
    try:
        results = target_cal.search(todo=True, uid=task.uid)
        if results:
            results[0].data = ical
            results[0].save()
        else:
            target_cal.add_todo(ical)
        return True
    except Exception as e:
        log.error("[CalDAV] Write failed for %s: %s", task.uid[:30], e)
        return False


def delete_caldav(calendars: list, uid: str) -> bool:
    """Delete a VTODO from CalDAV by UID. Searches all calendars."""
    for cal in calendars:
        try:
            results = cal.search(todo=True, uid=uid)
            for r in results:
                r.delete()
                log.info("[CalDAV] Deleted %s from '%s'", uid[:30], cal.name)
                return True
        except Exception:
            continue
    log.warning("[CalDAV] Could not find %s to delete", uid[:30])
    return False
