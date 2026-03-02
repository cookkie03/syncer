# vtodo-notion Refactor: Snapshot & Reconcile Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rewrite vtodo-notion/sync.py from scratch using a Snapshot & Reconcile architecture that eliminates ping-pong loops, handles deletions bidirectionally, and produces identical CalDAV/Notion mirrors every cycle.

**Architecture:** Each sync cycle takes a full snapshot of both CalDAV and Notion, categorizes every UID into 4 buckets (both/caldav-only/notion-only/vanished), then applies reconciliation rules. Content hashes detect changes; timestamps resolve conflicts. State file tracks known UIDs + hashes to detect deletions.

**Tech Stack:** Python 3.12, caldav, vobject, notion-client, python-dateutil, supercronic (cron)

**Key files:**
- Rewrite: `vtodo-notion/sync.py` (current: ~1100 lines → target: ~550 lines)
- Keep unchanged: `vtodo-notion/Dockerfile`, `vtodo-notion/entrypoint.sh`, `docker-compose.yml`
- Reference design: `docs/plans/2026-03-01-vtodo-notion-refactor-design.md`

---

### Task 1: Backup current sync.py and scaffold new file

**Files:**
- Backup: `vtodo-notion/sync_old.py` (copy of current)
- Rewrite: `vtodo-notion/sync.py`

**Step 1: Backup the current file**

```bash
cp vtodo-notion/sync.py vtodo-notion/sync_old.py
```

**Step 2: Write the new sync.py scaffold with Config, Logging, and Data Model sections**

Write `vtodo-notion/sync.py` with:

```python
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
    # Notion-only fields (populated when source is Notion)
    notion_page_id: str | None = None

    def content_hash(self) -> str:
        """Hash of semantic fields only (no timestamps, no page_id)."""
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
            # Auto-migrate from old format
            if "caldav_modified" in data and "known_uids" not in data:
                log.info("[State] Migrating from old format (caldav_modified) to new (known_uids)")
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
```

**Step 3: Verify syntax**

```bash
docker cp vtodo-notion/sync.py syncer-vtodo-notion-1:/app/sync.py
MSYS_NO_PATHCONV=1 docker compose exec vtodo-notion python3 -c "import py_compile; py_compile.compile('/app/sync.py', doraise=True); print('OK')"
```
Expected: `OK`

**Step 4: Commit**

```bash
git add vtodo-notion/sync_old.py vtodo-notion/sync.py
git commit -m "refactor(vtodo-notion): scaffold new sync.py with data model and state migration"
```

---

### Task 2: CalDAV Layer — fetch, parse, write, delete

**Files:**
- Modify: `vtodo-notion/sync.py` — append after Data Model section

**Step 1: Add the notification helper and CalDAV functions**

Append to sync.py:

```python
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

    priority_raw = int(vtodo_comp.priority.value) if hasattr(vtodo_comp, "priority") else 0
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
                task._caldav_obj = todo  # stash for delete/update later

                # Dedup: prefer active over completed
                existing = snapshot.get(task.uid)
                if existing:
                    if existing.is_completed and not task.is_completed:
                        log.info("[CalDAV] Duplicate UID %s: prefer active from '%s'", task.uid[:30], name)
                        snapshot[task.uid] = task
                    # else: keep existing (either both active, or existing is active)
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
    """Delete a VTODO from CalDAV by UID. Returns True on success."""
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
```

**Step 2: Verify syntax**

```bash
docker cp vtodo-notion/sync.py syncer-vtodo-notion-1:/app/sync.py
MSYS_NO_PATHCONV=1 docker compose exec vtodo-notion python3 -c "import py_compile; py_compile.compile('/app/sync.py', doraise=True); print('OK')"
```

**Step 3: Commit**

```bash
git add vtodo-notion/sync.py
git commit -m "refactor(vtodo-notion): add CalDAV layer — fetch, parse, write, delete"
```

---

### Task 3: Notion Layer — fetch, parse, write, archive

**Files:**
- Modify: `vtodo-notion/sync.py` — append after CalDAV Layer

**Step 1: Add Notion functions**

Append to sync.py:

```python
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
    """Parse a Notion page into a TaskData."""
    props = page.get("properties", {})

    summary = ""
    if props.get("Name") and props["Name"].get("title"):
        summary = props["Name"]["title"][0].get("text", {}).get("content", "")

    due = None
    if props.get("Scadenza") and props["Scadenza"].get("date"):
        due = props["Scadenza"]["date"].get("start", "")
        if due:
            due = due[:10]  # YYYY-MM-DD only

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
    """Fetch all Notion pages, return dict[UID, TaskData]. Pages without UID are skipped."""
    snapshot: dict[str, TaskData] = {}
    has_more = True
    cursor = None

    while has_more:
        try:
            params = {"database_id": database_id}
            if cursor:
                params["start_cursor"] = cursor
            resp = notion.databases.query(**params)
            for page in resp.get("results", []):
                task = parse_notion_page(page)
                if task.uid:
                    snapshot[task.uid] = task
            has_more = resp.get("has_more", False)
            cursor = resp.get("next_cursor")
        except Exception as e:
            log.error("[Notion] Fetch error: %s", e)
            break

    log.info("[Notion] Snapshot: %d pages with UIDs", len(snapshot))
    return snapshot


def build_notion_props(task: TaskData) -> dict:
    """Build Notion page properties from TaskData."""
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
    """Archive (soft-delete) a Notion page."""
    try:
        notion.pages.update(page_id=page_id, archived=True)
        return True
    except Exception as e:
        log.error("[Notion] Archive failed for page %s: %s", page_id, e)
        return False
```

**Step 2: Verify syntax**

Same command as before.

**Step 3: Commit**

```bash
git add vtodo-notion/sync.py
git commit -m "refactor(vtodo-notion): add Notion layer — fetch, parse, write, archive"
```

---

### Task 4: RRULE Engine

**Files:**
- Modify: `vtodo-notion/sync.py` — append after Notion Layer

**Step 1: Add RRULE functions**

Append to sync.py:

```python
# ── RRULE Engine ──────────────────────────────────────────────────────────

def next_future_occurrence(rrule_str: str, base_due: str | None) -> str | None:
    """Compute next RRULE occurrence >= today. Returns YYYY-MM-DD or None.

    Synology CalDAV keeps the original base DUE on recurring VTODOs
    and never advances it. This function computes what the user should
    actually see as the next deadline.
    """
    if not rrule_str or not base_due:
        return None
    try:
        base = date.fromisoformat(base_due[:10])
        today = date.today()
        if base >= today:
            return None  # already in the future

        dtstart = datetime(base.year, base.month, base.day)
        rule = rrulestr(rrule_str, dtstart=dtstart, ignoretz=True)
        # Search from yesterday to include today
        search_from = datetime(today.year, today.month, today.day) - timedelta(days=1)
        nxt = rule.after(search_from)
        if nxt:
            return nxt.date().isoformat()
    except Exception as e:
        log.warning("[RRULE] next_future_occurrence failed: %s", e)
    return None


def next_occurrence_after(rrule_str: str, from_due: str | None) -> str | None:
    """Compute the next RRULE occurrence after from_due. Returns YYYY-MM-DD or None.

    Used when completing a recurring task: advance DUE to the next occurrence.
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
        log.warning("[RRULE] next_occurrence_after failed: %s", e)
    return None
```

**Step 2: Verify syntax, commit**

```bash
git add vtodo-notion/sync.py
git commit -m "refactor(vtodo-notion): add RRULE engine"
```

---

### Task 5: Reconciler — the core algorithm

**Files:**
- Modify: `vtodo-notion/sync.py` — append after RRULE Engine

**Step 1: Add timestamp parser and reconcile function**

Append to sync.py:

```python
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
    """Returns True if CalDAV has the more recent modification."""
    ct = _parse_ts(caldav_task.last_modified)
    nt = _parse_ts(notion_task.last_modified)
    if ct and nt:
        return ct > nt
    return True  # default: CalDAV wins if timestamps unclear


def reconcile(
    caldav_snap: dict[str, TaskData],
    notion_snap: dict[str, TaskData],
    state: SyncState,
    notion: Client,
    database_id: str,
    calendars: list,
) -> dict[str, int]:
    """Core reconciliation loop. Returns stats dict."""
    stats = {
        "created_notion": 0, "created_caldav": 0,
        "updated_notion": 0, "updated_caldav": 0,
        "archived_notion": 0, "deleted_caldav": 0,
        "skipped": 0, "errors": 0,
        "recurring_advanced": 0,
    }
    consecutive_errors = 0
    new_known: dict[str, str] = {}

    all_uids = set(caldav_snap) | set(notion_snap) | set(state.known_uids)
    is_first_run = len(state.known_uids) == 0

    for uid in sorted(all_uids):
        # Circuit breaker
        if consecutive_errors >= CIRCUIT_BREAKER_THRESHOLD:
            log.error("[Reconcile] Circuit breaker: %d consecutive errors, stopping", consecutive_errors)
            notify("vtodo-notion: circuit breaker", f"{consecutive_errors} errori consecutivi. Controlla i log.")
            break

        in_caldav = uid in caldav_snap
        in_notion = uid in notion_snap
        was_known = uid in state.known_uids

        try:
            # ── BOTH SIDES ────────────────────────────────────────────
            if in_caldav and in_notion:
                ct = caldav_snap[uid]
                nt = notion_snap[uid]

                # Handle recurring task completed on CalDAV
                # (Synology transitioning — don't propagate COMPLETED to Notion)
                if ct.is_completed and ct.rrule:
                    ct = _handle_recurring_completed_caldav(ct)

                # Handle recurring task completed on Notion
                if nt.is_completed and nt.rrule:
                    ok = _handle_recurring_completed_notion(nt, ct, notion, database_id, calendars)
                    if ok:
                        stats["recurring_advanced"] += 1
                        new_known[uid] = ct.content_hash()
                        consecutive_errors = 0
                        continue
                    else:
                        stats["errors"] += 1
                        consecutive_errors += 1
                        new_known[uid] = state.known_uids.get(uid, ct.content_hash())
                        continue

                # Handle non-recurring task completed on Notion
                if nt.is_completed and not nt.rrule:
                    ok = _handle_oneshot_completed_notion(nt, ct, notion, calendars)
                    if ok:
                        stats["archived_notion"] += 1
                        consecutive_errors = 0
                    else:
                        stats["errors"] += 1
                        consecutive_errors += 1
                    # Don't add to new_known: task is gone
                    continue

                # Handle non-recurring task completed on CalDAV
                if ct.is_completed and not ct.rrule:
                    if nt.notion_page_id:
                        if archive_notion(notion, nt.notion_page_id):
                            log.info("[Sync] Archived completed one-shot: %s", uid[:30])
                            stats["archived_notion"] += 1
                            consecutive_errors = 0
                        else:
                            stats["errors"] += 1
                            consecutive_errors += 1
                    # Don't add to new_known: completed tasks shouldn't persist
                    continue

                # Normal case: compare content hashes
                # For recurring tasks: compute display DUE for comparison
                caldav_display = _with_display_due(ct) if ct.rrule else ct

                if caldav_display.content_hash() == nt.content_hash():
                    stats["skipped"] += 1
                    new_known[uid] = caldav_display.content_hash()
                    continue

                # Content differs: resolve conflict by timestamp
                if _caldav_wins(ct, nt):
                    task_to_write = caldav_display
                    if write_notion(notion, database_id, task_to_write, nt.notion_page_id):
                        log.info("[Sync] Updated Notion (CalDAV wins): %s", uid[:30])
                        stats["updated_notion"] += 1
                        consecutive_errors = 0
                    else:
                        stats["errors"] += 1
                        consecutive_errors += 1
                else:
                    # Notion wins: write to CalDAV, but restore original DUE for recurring
                    task_to_write = TaskData(**{k: getattr(nt, k) for k in TaskData.__dataclass_fields__})
                    if nt.rrule and ct.due:
                        task_to_write.due = ct.due  # preserve RRULE base date
                    task_to_write.is_completed = False  # never write COMPLETED for active
                    if write_caldav(calendars, task_to_write):
                        log.info("[Sync] Updated CalDAV (Notion wins): %s", uid[:30])
                        stats["updated_caldav"] += 1
                        consecutive_errors = 0
                    else:
                        stats["errors"] += 1
                        consecutive_errors += 1

                new_known[uid] = caldav_display.content_hash()

            # ── CALDAV ONLY ───────────────────────────────────────────
            elif in_caldav and not in_notion:
                ct = caldav_snap[uid]

                if was_known and not is_first_run:
                    # Was in both last time, now gone from Notion → deleted from Notion
                    if delete_caldav(calendars, uid):
                        log.info("[Sync] Deleted from CalDAV (removed from Notion): %s", uid[:30])
                        stats["deleted_caldav"] += 1
                        consecutive_errors = 0
                    else:
                        stats["errors"] += 1
                        consecutive_errors += 1
                    continue  # don't add to new_known

                # New on CalDAV: skip completed non-recurring (don't create in Notion)
                if ct.is_completed and not ct.rrule:
                    stats["skipped"] += 1
                    continue

                # Handle recurring with completed status
                if ct.is_completed and ct.rrule:
                    ct = _handle_recurring_completed_caldav(ct)

                # Compute display DUE for recurring
                task_to_write = _with_display_due(ct) if ct.rrule else ct

                if write_notion(notion, database_id, task_to_write):
                    log.info("[Sync] Created in Notion: %s '%s'", uid[:30], ct.summary[:30])
                    stats["created_notion"] += 1
                    consecutive_errors = 0
                else:
                    stats["errors"] += 1
                    consecutive_errors += 1

                new_known[uid] = task_to_write.content_hash()

            # ── NOTION ONLY ───────────────────────────────────────────
            elif not in_caldav and in_notion:
                nt = notion_snap[uid]

                if was_known and not is_first_run:
                    # Was in both last time, now gone from CalDAV → deleted from CalDAV
                    if nt.notion_page_id and archive_notion(notion, nt.notion_page_id):
                        log.info("[Sync] Archived in Notion (removed from CalDAV): %s", uid[:30])
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

            # ── VANISHED (was known, now gone from both) ──────────────
            else:
                log.debug("[Sync] UID %s vanished from both sides, cleaning state", uid[:30])
                # Don't add to new_known

        except Exception as e:
            log.error("[Sync] Unexpected error processing %s: %s", uid[:30], e)
            stats["errors"] += 1
            consecutive_errors += 1
            # Preserve in known to avoid accidental deletion next cycle
            if uid in state.known_uids:
                new_known[uid] = state.known_uids[uid]

    state.known_uids = new_known
    return stats


def _handle_recurring_completed_caldav(task: TaskData) -> TaskData:
    """A recurring task has STATUS:COMPLETED on CalDAV — Synology is transitioning.
    Return a copy with status reset to active."""
    t = TaskData(**{k: getattr(task, k) for k in TaskData.__dataclass_fields__})
    t.status = "In corso"
    t.is_completed = False
    return t


def _with_display_due(task: TaskData) -> TaskData:
    """For recurring tasks, compute the next future DUE for display in Notion."""
    if not task.rrule or not task.due:
        return task
    future = next_future_occurrence(task.rrule, task.due)
    if future:
        t = TaskData(**{k: getattr(task, k) for k in TaskData.__dataclass_fields__})
        t.due = future
        return t
    return task


def _handle_recurring_completed_notion(
    notion_task: TaskData, caldav_task: TaskData,
    notion: Client, database_id: str, calendars: list,
) -> bool:
    """Handle a recurring task marked as Done in Notion.
    Advance DUE to next occurrence, reset checkbox, keep active."""
    uid = notion_task.uid

    # Compute next DUE from RRULE
    base_due = caldav_task.due or notion_task.due
    new_due = next_occurrence_after(notion_task.rrule, base_due)
    if not new_due:
        # Fallback: advance by 1 day
        try:
            d = date.fromisoformat((base_due or "")[:10])
            new_due = (d + timedelta(days=1)).isoformat()
            log.warning("[RRULE] Exhausted for %s, advancing by 1 day", uid[:30])
        except Exception:
            log.error("[RRULE] Cannot advance DUE for %s", uid[:30])
            return False

    log.info("[Sync] Recurring completed: advancing DUE %s -> %s for %s",
             base_due, new_due, uid[:30])

    # Update Notion: reset to Not started, set new display DUE
    display_due = next_future_occurrence(notion_task.rrule, new_due) or new_due
    updated = TaskData(**{k: getattr(notion_task, k) for k in TaskData.__dataclass_fields__})
    updated.is_completed = False
    updated.status = "In corso"
    updated.due = display_due

    ok_notion = write_notion(notion, database_id, updated, notion_task.notion_page_id)

    # Update CalDAV: advance base DUE, keep NEEDS-ACTION (never COMPLETED for recurring)
    caldav_updated = TaskData(**{k: getattr(caldav_task, k) for k in TaskData.__dataclass_fields__})
    caldav_updated.due = new_due
    caldav_updated.is_completed = False
    caldav_updated.status = "In corso"
    ok_caldav = write_caldav(calendars, caldav_updated)

    return ok_notion and ok_caldav


def _handle_oneshot_completed_notion(
    notion_task: TaskData, caldav_task: TaskData,
    notion: Client, calendars: list,
) -> bool:
    """Handle a non-recurring task completed in Notion.
    Write COMPLETED to CalDAV and archive Notion page."""
    uid = notion_task.uid

    # Write COMPLETED to CalDAV
    completed = TaskData(**{k: getattr(caldav_task, k) for k in TaskData.__dataclass_fields__})
    completed.is_completed = True
    completed.status = "Completato"
    ok_caldav = write_caldav(calendars, completed)

    # Archive Notion page
    ok_notion = True
    if notion_task.notion_page_id:
        ok_notion = archive_notion(notion, notion_task.notion_page_id)
        if ok_notion:
            log.info("[Sync] One-shot completed and archived: %s", uid[:30])

    return ok_caldav and ok_notion
```

**Step 2: Verify syntax, commit**

```bash
git add vtodo-notion/sync.py
git commit -m "refactor(vtodo-notion): add Reconciler — core snapshot-diff algorithm"
```

---

### Task 6: Cleanup and Main orchestrator

**Files:**
- Modify: `vtodo-notion/sync.py` — append after Reconciler

**Step 1: Add cleanup and sync() orchestrator**

Append to sync.py:

```python
# ── Cleanup ───────────────────────────────────────────────────────────────

def cleanup_completed_recurring(client: caldav.DAVClient, max_age_days: int = RECURRING_CLEANUP_DAYS) -> int:
    """Delete completed instances of recurring VTODOs older than max_age_days."""
    deleted = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    try:
        principal = client.principal()
        calendars = principal.calendars()
    except Exception as e:
        log.error("[Cleanup] Cannot connect: %s", e)
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

                # Get completed timestamp
                completed_dt = None
                for attr in ("completed", "last_modified"):
                    if hasattr(vtodo, attr):
                        val = getattr(vtodo, attr).value
                        if isinstance(val, (datetime, date)):
                            completed_dt = val
                            break

                if completed_dt is None:
                    continue
                if hasattr(completed_dt, "tzinfo") and completed_dt.tzinfo is None:
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

def sync():
    log.info("=" * 60)
    log.info("Starting sync CalDAV <-> Notion")
    log.info("=" * 60)

    state = load_state()

    # Connect
    client = caldav.DAVClient(url=CALDAV_URL, username=CALDAV_USERNAME, password=CALDAV_PASSWORD)
    notion = Client(auth=NOTION_TOKEN)
    calendars = client.principal().calendars()

    # Snapshot both sides
    caldav_snap = fetch_caldav_snapshot(client)
    notion_snap = fetch_notion_snapshot(notion, NOTION_DATABASE_ID)

    # Reconcile
    stats = reconcile(caldav_snap, notion_snap, state, notion, NOTION_DATABASE_ID, calendars)

    # Save state
    state.last_sync = datetime.now(timezone.utc).isoformat()
    save_state(state)

    # Cleanup old completed recurring
    cleanup_completed_recurring(client)

    # Log summary
    log.info("-" * 60)
    log.info("Sync complete: "
             "created_notion=%d created_caldav=%d "
             "updated_notion=%d updated_caldav=%d "
             "archived_notion=%d deleted_caldav=%d "
             "recurring_advanced=%d skipped=%d errors=%d",
             stats["created_notion"], stats["created_caldav"],
             stats["updated_notion"], stats["updated_caldav"],
             stats["archived_notion"], stats["deleted_caldav"],
             stats["recurring_advanced"], stats["skipped"], stats["errors"])
    log.info("=" * 60)

    if stats["errors"] > 0:
        total = sum(stats.values())
        if total > 0 and stats["errors"] / total > 0.2:
            notify("vtodo-notion: errori sync",
                   f"{stats['errors']} errori su {total} operazioni. Controlla i log.")


if __name__ == "__main__":
    try:
        sync()
    except Exception as e:
        log.error("Fatal: %s", e)
        notify("vtodo-notion: errore fatale", f"Il sync si e' interrotto: {e}")
        raise
```

**Step 2: Verify syntax**

```bash
docker cp vtodo-notion/sync.py syncer-vtodo-notion-1:/app/sync.py
MSYS_NO_PATHCONV=1 docker compose exec vtodo-notion python3 -c "import py_compile; py_compile.compile('/app/sync.py', doraise=True); print('OK')"
```

**Step 3: Commit**

```bash
git add vtodo-notion/sync.py
git commit -m "refactor(vtodo-notion): add cleanup + main orchestrator — complete rewrite"
```

---

### Task 7: Fix TaskData._caldav_obj stash (remove non-serializable field)

**Files:**
- Modify: `vtodo-notion/sync.py`

**Step 1: Remove the _caldav_obj stash from fetch_caldav_snapshot**

The `task._caldav_obj = todo` line sets an attribute on a dataclass that isn't a declared field. This works in Python but is fragile. Since we never actually use this stash (delete_caldav searches by UID), remove it:

In `fetch_caldav_snapshot()`, remove the line:
```python
task._caldav_obj = todo  # stash for delete/update later
```

**Step 2: Verify and commit**

```bash
git add vtodo-notion/sync.py
git commit -m "fix(vtodo-notion): remove unused _caldav_obj stash from snapshot"
```

---

### Task 8: Integration test — dry run against live services

**Files:** None (testing only)

**Step 1: Copy new sync.py to container**

```bash
docker cp vtodo-notion/sync.py syncer-vtodo-notion-1:/app/sync.py
```

**Step 2: Run first sync cycle**

```bash
MSYS_NO_PATHCONV=1 docker compose exec vtodo-notion python3 /app/sync.py
```

**Expected behavior on first run (state migration from old format):**
- State auto-migrates (known_uids starts empty)
- All CalDAV active tasks appear as "Created in Notion" OR "skipped" (if already in Notion)
- All Notion-only tasks appear as "Created in CalDAV"
- No deletions on first run (is_first_run safety)
- Log ends with summary line

**Step 3: Run second sync cycle immediately**

```bash
MSYS_NO_PATHCONV=1 docker compose exec vtodo-notion python3 /app/sync.py
```

**Expected:** All items should be `skipped` (content identical). Zero creates, zero updates, zero errors.

**Step 4: Run third sync cycle**

Same command. Expected: identical to second run (stable state).

**Step 5: Verify state file**

```bash
MSYS_NO_PATHCONV=1 docker compose exec vtodo-notion python3 -c "
import json
with open('/data/sync_state.json') as f:
    state = json.load(f)
print('known_uids:', len(state.get('known_uids', {})))
print('last_sync:', state.get('last_sync'))
"
```

Expected: `known_uids` count matches the number of active synced tasks (~116-118).

**Step 6: Commit if passing**

```bash
git add vtodo-notion/sync.py
git commit -m "test(vtodo-notion): verified 3 stable sync cycles with new reconciler"
```

---

### Task 9: Edge case testing — create, modify, complete, delete

**Step 1: Test creation propagation (CalDAV → Notion)**

Create a test task on CalDAV via the sync container:

```bash
MSYS_NO_PATHCONV=1 docker compose exec vtodo-notion python3 -c "
import os, caldav
c = caldav.DAVClient(url=os.environ['CALDAV_URL'], username=os.environ['CALDAV_USERNAME'], password=os.environ['CALDAV_PASSWORD'])
cal = [x for x in c.principal().calendars() if 'Promemoria' in str(x.name)][0]
cal.add_todo('''BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VTODO
UID:test-refactor-001@vtodo-notion
SUMMARY:TEST Refactor Sync
STATUS:NEEDS-ACTION
PRIORITY:5
DUE:20260305
END:VTODO
END:VCALENDAR''')
print('Created test task')
"
```

Run sync. Expected: `created_notion=1` in the summary.

**Step 2: Test modification propagation (modify on CalDAV, verify Notion updates)**

Modify the test task summary on CalDAV, run sync. Expected: `updated_notion=1`.

**Step 3: Test completion propagation (complete on Notion, verify CalDAV)**

Mark the test task as "Done" in Notion manually, then run sync.
Expected: `archived_notion=1` (one-shot completed → archived in Notion, COMPLETED in CalDAV).

**Step 4: Test deletion propagation**

Create another test task, sync (so it's in both). Then delete from CalDAV.
Run sync. Expected: `archived_notion=1` (deleted from CalDAV → archived in Notion).

**Step 5: Clean up test tasks**

Delete test tasks from both platforms.

**Step 6: Commit**

```bash
git commit --allow-empty -m "test(vtodo-notion): edge cases verified — create/modify/complete/delete"
```

---

### Task 10: Rebuild Docker image and verify scheduled execution

**Step 1: Rebuild**

```bash
cd /c/Users/lucam/OneDrive/Github/syncer
docker compose build vtodo-notion
docker compose up -d vtodo-notion
```

**Step 2: Wait for first scheduled cycle (10 minutes)**

```bash
docker compose logs -f vtodo-notion --tail 5
```

Expected: sync runs automatically, all items skipped (stable).

**Step 3: Verify 2-3 scheduled cycles are stable**

Check logs after 20-30 minutes. All cycles should show zero creates/updates/deletes.

**Step 4: Remove backup file**

```bash
rm vtodo-notion/sync_old.py
git add -A
git commit -m "chore(vtodo-notion): remove old sync.py backup after verified refactor"
```

---

## Summary of changes

| What | Before | After |
|---|---|---|
| Architecture | Patch-on-patch, two separate loops | Snapshot & Reconcile, single reconciliation pass |
| Lines of code | ~1100 | ~550 |
| Deletion handling | None | Bidirectional (state-based detection) |
| Ping-pong prevention | Content comparison + timestamp | Content hash + single-pass writes |
| Recurring tasks | force_update + DUE restoration + dedup | Clean: display DUE for Notion, base DUE for CalDAV |
| State file | 473 stale UIDs, timestamp maps | Clean known_uids + content hashes |
| Duplicate UIDs | Inline dedup in loop | Pre-dedup in snapshot fetch |
| Circuit breaker | Global variables | Local counter in reconcile loop |
