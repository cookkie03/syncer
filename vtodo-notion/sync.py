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

import uuid

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


def _pick_best(a: TaskData, b: TaskData) -> TaskData:
    """Pick the best instance when two share the same UID.
    Priority: active > completed, then last_modified, then due furthest in future."""
    if a.is_completed != b.is_completed:
        return b if a.is_completed else a
    if a.last_modified != b.last_modified:
        return b if (b.last_modified or "") > (a.last_modified or "") else a
    return b if (b.due or "") > (a.due or "") else a


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

                # Dedup: pick best instance per UID
                existing = snapshot.get(task.uid)
                if existing:
                    winner = _pick_best(existing, task)
                    if winner is not existing:
                        log.info("[CalDAV] Duplicate UID %s: replacing with better instance from '%s'", task.uid[:30], name)
                    snapshot[task.uid] = winner
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


# ── Notion Layer ──────────────────────────────────────────────────────────

def _get_rt(props: dict, key: str) -> str:
    """Extract rich_text property value."""
    p = props.get(key)
    if p and p.get("rich_text"):
        return p["rich_text"][0].get("text", {}).get("content", "")
    return ""


def _get_sel(props: dict, key: str, default: str = "") -> str:
    """Extract select property value."""
    p = props.get(key)
    if p and p.get("select"):
        return p["select"].get("name", default)
    return default


def parse_notion_page(page: dict) -> TaskData:
    """Parse a Notion page dict into a TaskData."""
    props = page.get("properties", {})

    summary = ""
    if props.get("Name") and props["Name"].get("title"):
        summary = props["Name"]["title"][0].get("text", {}).get("content", "")

    due = None
    if props.get("Scadenza") and props["Scadenza"].get("date"):
        raw_due = props["Scadenza"]["date"].get("start", "")
        if raw_due:
            due = raw_due[:10]  # YYYY-MM-DD only

    is_completed = False
    if props.get("Completato"):
        is_completed = props["Completato"].get("status", {}).get("name", "") == "Done"

    return TaskData(
        uid=_get_rt(props, "UID CalDAV"),
        summary=summary,
        description=_get_rt(props, "Descrizione"),
        due=due,
        priority=_get_sel(props, "Priorità", "Nessuna"),
        status="Completato" if is_completed else "In corso",
        is_completed=is_completed,
        location=_get_rt(props, "Luogo"),
        url=(props.get("URL") or {}).get("url") or "",
        rrule=_get_rt(props, "Periodicità"),
        list_name=_get_sel(props, "Lista"),
        last_modified=page.get("last_edited_time", ""),
        notion_page_id=page["id"],
    )


def fetch_notion_snapshot(notion: Client, database_id: str) -> dict[str, TaskData]:
    """Fetch all active Notion pages. Assigns a UID to pages missing one."""
    snapshot: dict[str, TaskData] = {}
    has_more = True
    cursor = None

    while has_more:
        try:
            params: dict[str, Any] = {"database_id": database_id}
            if cursor:
                params["start_cursor"] = cursor
            resp = notion.databases.query(**params)
            for page in resp.get("results", []):
                task = parse_notion_page(page)
                if not task.uid:
                    # New page created manually in Notion — assign a UUID
                    new_uid = str(uuid.uuid4()).upper()
                    try:
                        notion.pages.update(
                            page_id=page["id"],
                            properties={"UID CalDAV": {"rich_text": [{"text": {"content": new_uid}}]}},
                        )
                        task.uid = new_uid
                        log.info("[Notion] Assigned UID %s to page '%s'", new_uid[:8], task.summary[:30])
                    except Exception as e:
                        log.error("[Notion] Failed to assign UID to '%s': %s", task.summary[:30], e)
                        continue
                # Dedup: if UID already seen, keep best and archive loser
                existing = snapshot.get(task.uid)
                if existing:
                    winner = _pick_best(existing, task)
                    loser = task if winner is existing else existing
                    if loser.notion_page_id:
                        log.info("[Notion] Duplicate UID %s: archiving '%s' (page %s)",
                                 task.uid[:30], loser.summary[:30], loser.notion_page_id[:8])
                        archive_notion(notion, loser.notion_page_id)
                    snapshot[task.uid] = winner
                else:
                    snapshot[task.uid] = task
            has_more = resp.get("has_more", False)
            cursor = resp.get("next_cursor")
        except Exception as e:
            log.error("[Notion] Fetch error: %s", e)
            break

    log.info("[Notion] Snapshot: %d pages", len(snapshot))
    return snapshot


def build_notion_props(task: TaskData) -> dict:
    """Build Notion page properties dict from TaskData."""
    props: dict[str, Any] = {
        "Name": {"title": [{"text": {"content": task.summary or "(senza titolo)"}}]},
        "UID CalDAV": {"rich_text": [{"text": {"content": task.uid}}]},
        "Completato": {"status": {"name": "Done" if task.is_completed else "Not started"}},
    }

    if task.description:
        props["Descrizione"] = {"rich_text": [{"text": {"content": task.description[:1990]}}]}
    if task.due:
        props["Scadenza"] = {"date": {"start": task.due[:10]}}
    if task.priority:
        props["Priorità"] = {"select": {"name": task.priority}}
    if task.location:
        props["Luogo"] = {"rich_text": [{"text": {"content": task.location}}]}
    if task.url:
        props["URL"] = {"url": task.url}
    if task.list_name:
        props["Lista"] = {"select": {"name": task.list_name}}
    if task.rrule:
        props["Periodicità"] = {"rich_text": [{"text": {"content": task.rrule}}]}

    return props


def write_notion(notion: Client, database_id: str, task: TaskData, page_id: str | None = None) -> bool:
    """Create or update a Notion page. Returns True on success."""
    props = build_notion_props(task)
    try:
        if page_id:
            notion.pages.update(page_id=page_id, properties=props)
        else:
            notion.pages.create(parent={"database_id": database_id}, properties=props)
        return True
    except Exception as e:
        log.error("[Notion] Write failed for %s: %s", task.uid[:30], e)
        return False


def archive_notion(notion: Client, page_id: str) -> bool:
    """Archive (soft-delete) a Notion page. Returns True on success."""
    try:
        notion.pages.update(page_id=page_id, archived=True)
        return True
    except Exception as e:
        log.error("[Notion] Archive failed for page %s: %s", page_id, e)
        return False


# ── RRULE Engine ──────────────────────────────────────────────────────────

def next_future_occurrence(rrule_str: str, base_due: str | None) -> str | None:
    """Compute next RRULE occurrence >= today from base_due. Returns YYYY-MM-DD or None.

    Synology CalDAV keeps the original base DUE on recurring VTODOs and never
    advances it automatically. This function computes the correct next deadline
    for display in Notion.
    """
    if not rrule_str or not base_due:
        return None
    try:
        base = date.fromisoformat(base_due[:10])
        today = date.today()
        if base >= today:
            return None  # already in the future, no adjustment needed

        dtstart = datetime(base.year, base.month, base.day)
        rule = rrulestr(rrule_str, dtstart=dtstart, ignoretz=True)
        # Search from yesterday to correctly include today as a valid next occurrence
        search_from = datetime(today.year, today.month, today.day) - timedelta(days=1)
        nxt = rule.after(search_from)
        if nxt:
            return nxt.date().isoformat()
    except Exception as e:
        log.warning("[RRULE] next_future_occurrence failed (rrule=%r, base=%r): %s", rrule_str, base_due, e)
    return None


def next_occurrence_after(rrule_str: str, from_due: str | None) -> str | None:
    """Compute the next RRULE occurrence strictly after from_due. Returns YYYY-MM-DD or None.

    Used when completing a recurring task: advance DUE to the next occurrence
    without writing STATUS:COMPLETED to CalDAV (which kills the series on Synology).
    """
    if not rrule_str:
        return None
    try:
        from_date = date.fromisoformat(from_due[:10]) if from_due else date.today()
        dtstart = datetime(from_date.year, from_date.month, from_date.day)
        rule = rrulestr(rrule_str, dtstart=dtstart, ignoretz=True)
        nxt = rule.after(dtstart)
        if nxt:
            return nxt.date().isoformat()
    except Exception as e:
        log.warning("[RRULE] next_occurrence_after failed (rrule=%r, from=%r): %s", rrule_str, from_due, e)
    return None


DAY_ABBR = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _adjust_rrule_to_due(rrule_str: str, new_due: str) -> str:
    """Update RRULE day/month-day components to match a new DUE date.
    E.g. FREQ=WEEKLY;BYDAY=MO + DUE=Wednesday → FREQ=WEEKLY;BYDAY=WE"""
    if not rrule_str or not new_due:
        return rrule_str
    try:
        d = date.fromisoformat(new_due[:10])
    except (ValueError, TypeError):
        return rrule_str

    parts = rrule_str.split(";")
    new_parts = []
    for part in parts:
        key, _, val = part.partition("=")
        if key == "BYDAY" and "FREQ=WEEKLY" in rrule_str:
            new_parts.append(f"BYDAY={DAY_ABBR[d.weekday()]}")
        elif key == "BYMONTHDAY" and "FREQ=MONTHLY" in rrule_str:
            new_parts.append(f"BYMONTHDAY={d.day}")
        else:
            new_parts.append(part)
    return ";".join(new_parts)


# ── Reconciler ────────────────────────────────────────────────────────────

def _parse_ts(ts: str | None) -> datetime | None:
    """Parse ISO timestamp to naive UTC datetime for comparison."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _caldav_wins(caldav_task: TaskData, notion_task: TaskData) -> bool:
    """Returns True if CalDAV has a more recent modification than Notion."""
    ct = _parse_ts(caldav_task.last_modified)
    nt = _parse_ts(notion_task.last_modified)
    if ct and nt:
        return ct > nt
    return True  # default: CalDAV wins when timestamps are ambiguous


def _clone(task: TaskData, **overrides) -> TaskData:
    """Return a copy of TaskData with optional field overrides."""
    return TaskData(**{**{f: getattr(task, f) for f in task.__dataclass_fields__}, **overrides})


def _handle_recurring_completed_caldav(task: TaskData) -> TaskData:
    """A recurring task has STATUS:COMPLETED on CalDAV (Synology transitioning).
    Return a copy reset to active — never show COMPLETED recurring in Notion."""
    return _clone(task, status="In corso", is_completed=False)


def _with_display_due(task: TaskData) -> TaskData:
    """For recurring tasks: compute next future DUE for display in Notion.
    CalDAV keeps the original base DUE; Notion should show the next occurrence."""
    if not task.rrule or not task.due:
        return task
    future = next_future_occurrence(task.rrule, task.due)
    if future:
        return _clone(task, due=future)
    return task


def _handle_recurring_completed_notion(
    notion_task: TaskData,
    caldav_task: TaskData,
    notion: Client,
    database_id: str,
    calendars: list,
) -> bool:
    """Handle a recurring task marked Done in Notion.
    Advance DUE to next occurrence, reset checkbox, keep NEEDS-ACTION on CalDAV.
    Returns True on success."""
    uid = notion_task.uid

    # Compute next DUE from RRULE (use CalDAV base DUE as reference)
    base_due = caldav_task.due or notion_task.due
    new_due = next_occurrence_after(notion_task.rrule or caldav_task.rrule, base_due)
    if not new_due:
        # Fallback: advance by 1 day if RRULE exhausted
        try:
            d = date.fromisoformat((base_due or "")[:10])
            new_due = (d + timedelta(days=1)).isoformat()
            log.warning("[RRULE] Exhausted for %s, advancing DUE by 1 day", uid[:30])
        except Exception:
            log.error("[RRULE] Cannot advance DUE for %s", uid[:30])
            return False

    log.info("[Sync] Recurring completed: advancing DUE %s -> %s for %s", base_due, new_due, uid[:30])

    # Notion: reset to Not started, show computed future DUE
    display_due = next_future_occurrence(notion_task.rrule or caldav_task.rrule, new_due) or new_due
    ok_notion = write_notion(
        notion, database_id,
        _clone(notion_task, is_completed=False, status="In corso", due=display_due),
        notion_task.notion_page_id,
    )

    # CalDAV: advance base DUE, keep NEEDS-ACTION (never write COMPLETED for recurring)
    ok_caldav = write_caldav(
        calendars,
        _clone(caldav_task, due=new_due, is_completed=False, status="In corso"),
    )

    return ok_notion and ok_caldav


def _handle_oneshot_completed_notion(
    notion_task: TaskData,
    caldav_task: TaskData,
    notion: Client,
    calendars: list,
) -> bool:
    """Handle a non-recurring task completed in Notion.
    Write COMPLETED to CalDAV and archive the Notion page.
    Returns True on success."""
    uid = notion_task.uid

    ok_caldav = write_caldav(calendars, _clone(caldav_task, is_completed=True, status="Completato"))

    ok_notion = True
    if notion_task.notion_page_id:
        ok_notion = archive_notion(notion, notion_task.notion_page_id)
        if ok_notion:
            log.info("[Sync] One-shot completed and archived: %s '%s'", uid[:30], notion_task.summary[:30])

    return ok_caldav and ok_notion


def reconcile(
    caldav_snap: dict[str, TaskData],
    notion_snap: dict[str, TaskData],
    state: SyncState,
    notion: Client,
    database_id: str,
    calendars: list,
) -> dict[str, int]:
    """Core reconciliation loop.

    Categorizes every UID into buckets and applies rules:
    - Both sides: compare content hashes, resolve conflict by timestamp
    - CalDAV-only: new task (create in Notion) or deleted from Notion (delete from CalDAV)
    - Notion-only: new task (create in CalDAV) or deleted from CalDAV (archive in Notion)
    - Vanished from both: cleanup state entry

    First-run safety: if known_uids is empty, no deletions are propagated.
    """
    stats: dict[str, int] = {
        "created_notion": 0, "created_caldav": 0,
        "updated_notion": 0, "updated_caldav": 0,
        "archived_notion": 0, "deleted_caldav": 0,
        "recurring_advanced": 0, "skipped": 0, "errors": 0,
    }
    consecutive_errors = 0
    new_known: dict[str, str] = {}
    is_first_run = len(state.known_uids) == 0

    if is_first_run:
        log.info("[Reconcile] First run (empty state) — deletions disabled for safety")

    all_uids = set(caldav_snap) | set(notion_snap) | set(state.known_uids)
    log.info("[Reconcile] Processing %d unique UIDs", len(all_uids))

    for uid in sorted(all_uids):
        if consecutive_errors >= CIRCUIT_BREAKER_THRESHOLD:
            log.error("[Reconcile] Circuit breaker: %d consecutive errors — stopping cycle", consecutive_errors)
            notify("vtodo-notion: circuit breaker", f"{consecutive_errors} errori consecutivi. Controlla i log.")
            break

        in_caldav = uid in caldav_snap
        in_notion = uid in notion_snap
        was_known = uid in state.known_uids

        try:
            # ── BOTH SIDES ────────────────────────────────────────────────
            if in_caldav and in_notion:
                ct = caldav_snap[uid]
                nt = notion_snap[uid]

                # Recurring task COMPLETED on CalDAV: Synology is transitioning instances
                if ct.is_completed and ct.rrule:
                    ct = _handle_recurring_completed_caldav(ct)

                # Recurring task completed on Notion → advance DUE
                if nt.is_completed and nt.rrule:
                    ok = _handle_recurring_completed_notion(nt, ct, notion, database_id, calendars)
                    if ok:
                        stats["recurring_advanced"] += 1
                        consecutive_errors = 0
                    else:
                        stats["errors"] += 1
                        consecutive_errors += 1
                    new_known[uid] = state.known_uids.get(uid, ct.content_hash())
                    continue

                # Non-recurring task completed on Notion → COMPLETED on CalDAV + archive Notion
                if nt.is_completed and not nt.rrule:
                    ok = _handle_oneshot_completed_notion(nt, ct, notion, calendars)
                    if ok:
                        stats["archived_notion"] += 1
                        consecutive_errors = 0
                    else:
                        stats["errors"] += 1
                        consecutive_errors += 1
                    continue  # task is done, don't add to new_known

                # Non-recurring task COMPLETED on CalDAV → archive Notion
                if ct.is_completed and not ct.rrule:
                    if nt.notion_page_id and archive_notion(notion, nt.notion_page_id):
                        log.info("[Sync] Archived completed one-shot (CalDAV→Notion): %s", uid[:30])
                        stats["archived_notion"] += 1
                        consecutive_errors = 0
                    else:
                        stats["errors"] += 1
                        consecutive_errors += 1
                    continue  # task is done

                # Normal case: compute display version for comparison
                caldav_display = _with_display_due(ct) if ct.rrule else ct

                if caldav_display.content_hash() == nt.content_hash():
                    stats["skipped"] += 1
                    new_known[uid] = caldav_display.content_hash()
                    continue

                # Content differs: resolve by timestamp
                if _caldav_wins(ct, nt):
                    # If DUE changed on a recurring task, adjust RRULE on CalDAV too
                    if ct.rrule and ct.due:
                        adjusted_rrule = _adjust_rrule_to_due(ct.rrule, ct.due)
                        if adjusted_rrule != ct.rrule:
                            log.info("[Sync] Adjusting RRULE for %s: %s → %s", uid[:30], ct.rrule, adjusted_rrule)
                            write_caldav(calendars, _clone(ct, rrule=adjusted_rrule))
                            caldav_display = _clone(caldav_display, rrule=adjusted_rrule)
                    if write_notion(notion, database_id, caldav_display, nt.notion_page_id):
                        log.info("[Sync] Updated Notion (CalDAV wins): %s", uid[:30])
                        stats["updated_notion"] += 1
                        consecutive_errors = 0
                    else:
                        stats["errors"] += 1
                        consecutive_errors += 1
                else:
                    # Notion wins: write to CalDAV, adjust RRULE if DUE changed
                    task_to_write = _clone(nt, is_completed=False)
                    if nt.rrule and nt.due:
                        adjusted_rrule = _adjust_rrule_to_due(nt.rrule, nt.due)
                        task_to_write = _clone(task_to_write, rrule=adjusted_rrule)
                    if write_caldav(calendars, task_to_write):
                        log.info("[Sync] Updated CalDAV (Notion wins): %s", uid[:30])
                        stats["updated_caldav"] += 1
                        consecutive_errors = 0
                    else:
                        stats["errors"] += 1
                        consecutive_errors += 1

                new_known[uid] = caldav_display.content_hash()

            # ── CALDAV ONLY ───────────────────────────────────────────────
            elif in_caldav and not in_notion:
                ct = caldav_snap[uid]

                if was_known and not is_first_run:
                    # Was synced before, now gone from Notion → user deleted it from Notion
                    if delete_caldav(calendars, uid):
                        log.info("[Sync] Deleted from CalDAV (Notion deletion): %s", uid[:30])
                        stats["deleted_caldav"] += 1
                        consecutive_errors = 0
                    else:
                        stats["errors"] += 1
                        consecutive_errors += 1
                    continue  # don't add to new_known

                # Skip completed non-recurring: don't create in Notion
                if ct.is_completed and not ct.rrule:
                    stats["skipped"] += 1
                    continue

                # Reset recurring COMPLETED to active before creating in Notion
                if ct.is_completed and ct.rrule:
                    ct = _handle_recurring_completed_caldav(ct)

                task_to_write = _with_display_due(ct) if ct.rrule else ct
                if write_notion(notion, database_id, task_to_write):
                    log.info("[Sync] Created in Notion: %s '%s'", uid[:30], ct.summary[:30])
                    stats["created_notion"] += 1
                    consecutive_errors = 0
                else:
                    stats["errors"] += 1
                    consecutive_errors += 1

                new_known[uid] = task_to_write.content_hash()

            # ── NOTION ONLY ───────────────────────────────────────────────
            elif not in_caldav and in_notion:
                nt = notion_snap[uid]

                if was_known and not is_first_run:
                    # Was synced before, now gone from CalDAV → user deleted it from CalDAV
                    if nt.notion_page_id and archive_notion(notion, nt.notion_page_id):
                        log.info("[Sync] Archived in Notion (CalDAV deletion): %s", uid[:30])
                        stats["archived_notion"] += 1
                        consecutive_errors = 0
                    else:
                        stats["errors"] += 1
                        consecutive_errors += 1
                    continue  # don't add to new_known

                # New on Notion: create on CalDAV
                if write_caldav(calendars, nt):
                    log.info("[Sync] Created in CalDAV: %s '%s'", uid[:30], nt.summary[:30])
                    stats["created_caldav"] += 1
                    consecutive_errors = 0
                else:
                    stats["errors"] += 1
                    consecutive_errors += 1

                new_known[uid] = nt.content_hash()

            # ── VANISHED FROM BOTH ────────────────────────────────────────
            else:
                log.debug("[Reconcile] UID %s vanished from both sides — cleaning state", uid[:30])
                # Don't add to new_known: state entry removed

        except Exception as e:
            log.error("[Reconcile] Unexpected error for %s: %s", uid[:30], e)
            stats["errors"] += 1
            consecutive_errors += 1
            # Preserve in new_known to avoid accidental deletion next cycle
            if uid in state.known_uids:
                new_known[uid] = state.known_uids[uid]

    state.known_uids = new_known
    log.info("[Reconcile] Done. known_uids updated to %d entries", len(new_known))
    return stats

# ── Cleanup ───────────────────────────────────────────────────────────────

def cleanup_completed_recurring(client: caldav.DAVClient, max_age_days: int = RECURRING_CLEANUP_DAYS) -> int:
    """Delete completed instances of recurring VTODOs older than max_age_days.

    Synology creates a COMPLETED copy for each finished occurrence of a recurring
    task. These accumulate over time and confuse the sync. This routine auto-prunes
    them after max_age_days to keep CalDAV clean.
    """
    deleted = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    try:
        principal = client.principal()
        calendars = principal.calendars()
    except Exception as e:
        log.error("[Cleanup] Cannot connect to CalDAV: %s", e)
        return 0

    for cal in calendars:
        name = str(cal.name) if cal.name else "?"
        try:
            todos = cal.todos(include_completed=True)
        except Exception:
            continue

        for todo in todos:
            try:
                vtodo = todo.vobject_instance.vtodo
                status = str(vtodo.status.value).upper() if hasattr(vtodo, "status") else ""
                has_rrule = hasattr(vtodo, "rrule")

                if status != "COMPLETED" or not has_rrule:
                    continue

                # Get completed/last-modified timestamp
                completed_dt = None
                for attr in ("completed", "last_modified"):
                    if hasattr(vtodo, attr):
                        val = getattr(vtodo, attr).value
                        if isinstance(val, (datetime, date)):
                            completed_dt = val
                            break

                if completed_dt is None:
                    continue

                # Normalize to UTC-aware datetime
                if isinstance(completed_dt, date) and not isinstance(completed_dt, datetime):
                    completed_dt = datetime(completed_dt.year, completed_dt.month, completed_dt.day, tzinfo=timezone.utc)
                elif hasattr(completed_dt, "tzinfo") and completed_dt.tzinfo is None:
                    completed_dt = completed_dt.replace(tzinfo=timezone.utc)

                if completed_dt < cutoff:
                    todo.delete()
                    deleted += 1
            except Exception:
                pass

    if deleted:
        log.info("[Cleanup] Deleted %d completed recurring VTODOs (older than %d days)", deleted, max_age_days)
    return deleted


# ── Main ──────────────────────────────────────────────────────────────────

def sync() -> None:
    log.info("=" * 60)
    log.info("Starting sync CalDAV <-> Notion")
    log.info("=" * 60)

    state = load_state()

    # Connect to both services (with retry on network errors)
    client = caldav.DAVClient(url=CALDAV_URL, username=CALDAV_USERNAME, password=CALDAV_PASSWORD, timeout=60)
    notion = Client(auth=NOTION_TOKEN)
    calendars = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            calendars = client.principal().calendars()
            break
        except Exception as e:
            log.warning("[CalDAV] Connection attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
            if attempt == MAX_RETRIES:
                raise
            time.sleep(5 * attempt)

    # Snapshot both sides simultaneously
    caldav_snap = fetch_caldav_snapshot(client)
    notion_snap = fetch_notion_snapshot(notion, NOTION_DATABASE_ID)

    # Reconcile
    stats = reconcile(caldav_snap, notion_snap, state, notion, NOTION_DATABASE_ID, calendars)

    # Persist updated state
    state.last_sync = datetime.now(timezone.utc).isoformat()
    save_state(state)

    # Auto-cleanup old completed recurring VTODOs
    cleanup_completed_recurring(client)

    # Summary log
    log.info("-" * 60)
    log.info(
        "Sync complete | "
        "created_notion=%d created_caldav=%d | "
        "updated_notion=%d updated_caldav=%d | "
        "archived_notion=%d deleted_caldav=%d | "
        "recurring_advanced=%d skipped=%d errors=%d",
        stats["created_notion"], stats["created_caldav"],
        stats["updated_notion"], stats["updated_caldav"],
        stats["archived_notion"], stats["deleted_caldav"],
        stats["recurring_advanced"], stats["skipped"], stats["errors"],
    )
    log.info("=" * 60)

    # Notify on changes or errors
    total_ops = sum(v for k, v in stats.items() if k != "skipped")
    changes = total_ops - stats["errors"]
    if changes > 0:
        lines = []
        for key, emoji in [
            ("created_notion", "📥"), ("created_caldav", "📤"),
            ("updated_notion", "✏️"), ("updated_caldav", "✏️"),
            ("archived_notion", "🗃"), ("deleted_caldav", "🗑"),
            ("recurring_advanced", "🔄"),
        ]:
            if stats[key]:
                lines.append(f"{emoji} {key.replace('_', ' ')}: {stats[key]}")
        if stats["errors"]:
            lines.append(f"⚠️ errori: {stats['errors']}")
        notify("vtodo-notion sync", "\n".join(lines))
    elif stats["errors"] > 0:
        notify(
            "vtodo-notion: errori sync",
            f"{stats['errors']} errori su {total_ops} operazioni. Controlla i log.",
        )


if __name__ == "__main__":
    try:
        sync()
    except Exception as e:
        log.error("Fatal: %s", e)
        notify("vtodo-notion: errore fatale", f"Il sync si e' interrotto: {e}")
        raise
