#!/usr/bin/env python3
"""
vtodo-notion — Bidirectional CalDAV VTODO ↔ Notion sync
Supports two-way sync with conflict resolution based on last-modified timestamp.
"""

import os
import sys
import json
import logging
import time
from datetime import date, datetime, timedelta
from dateutil.rrule import rrulestr
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field

import requests
import caldav
from caldav.lib import error
from notion_client import Client
from notion_client.errors import APIResponseError


LOG_DIR = Path(os.environ.get("LOG_DIR", "/data/logs"))
LOG_FILE = LOG_DIR / "sync.log"

# Setup logging: file + stdout
LOG_DIR.mkdir(parents=True, exist_ok=True)

file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
))

# Also keep stdout
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))

# Configure root logger with both handlers
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
root_logger.addHandler(file_handler)
root_logger.addHandler(stdout_handler)

log = logging.getLogger("vtodo-notion")


@dataclass
class SyncState:
    caldav_modified: dict[str, str] = field(default_factory=dict)
    notion_modified: dict[str, str] = field(default_factory=dict)
    last_sync: str | None = None


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        log.error("Required environment variable %s is not set", name)
        sys.exit(1)
    return value


CALDAV_URL         = require_env("CALDAV_URL")
CALDAV_USERNAME    = require_env("CALDAV_USERNAME")
CALDAV_PASSWORD    = require_env("CALDAV_PASSWORD")
NOTION_TOKEN       = require_env("NOTION_TOKEN")
NOTION_DATABASE_ID = require_env("NOTION_DATABASE_ID")
STATE_FILE         = os.environ.get("STATE_FILE", "/data/sync_state.json")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")


def notify(title: str, message: str) -> None:
    """Send a Telegram message. No-op if credentials are not set."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    text = f"🔔 *{title}*\n\n{message}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as exc:
        log.warning("[notify] Failed to send Telegram message: %s", exc)


MAX_RETRIES = 3
CIRCUIT_BREAKER_THRESHOLD = 10
CIRCUIT_BREAKER_PAUSE = 300


circuit_breaker_errors = 0
circuit_breaker_triggered = False


def load_state() -> SyncState:
    try:
        if Path(STATE_FILE).exists():
            with open(STATE_FILE) as f:
                data = json.load(f)
                return SyncState(
                    caldav_modified=data.get("caldav_modified", {}),
                    notion_modified=data.get("notion_modified", {}),
                    last_sync=data.get("last_sync"),
                )
    except Exception as e:
        log.warning("Could not load state file: %s", e)
    return SyncState()


def save_state(state: SyncState) -> None:
    try:
        Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({
                "caldav_modified": state.caldav_modified,
                "notion_modified": state.notion_modified,
                "last_sync": state.last_sync,
            }, f, indent=2)
    except Exception as e:
        log.error("Could not save state file: %s", e)


def with_retry(func):
    def wrapper(*args, **kwargs):
        global circuit_breaker_errors, circuit_breaker_triggered

        if circuit_breaker_triggered:
            log.warning("Circuit breaker triggered, skipping operation")
            return None

        for attempt in range(MAX_RETRIES):
            try:
                result = func(*args, **kwargs)
                circuit_breaker_errors = 0
                return result
            except APIResponseError as e:
                # Errori di validazione (400): permanenti, inutile riprovare, non contare nel breaker
                if e.status == 400:
                    log.error("Validation error (not retrying): %s", e)
                    raise
                _handle_retry_error(e, attempt)
                if circuit_breaker_triggered:
                    return None
            except Exception as e:
                _handle_retry_error(e, attempt)
                if circuit_breaker_triggered:
                    return None
        return None
    return wrapper


def _handle_retry_error(e: Exception, attempt: int) -> None:
    global circuit_breaker_errors, circuit_breaker_triggered
    circuit_breaker_errors += 1
    if circuit_breaker_errors >= CIRCUIT_BREAKER_THRESHOLD:
        circuit_breaker_triggered = True
        log.error("Circuit breaker triggered after %d errors", circuit_breaker_errors)
        return
    if attempt < MAX_RETRIES - 1:
        wait_time = (2 ** attempt)
        log.warning("Attempt %d failed: %s. Retrying in %ds...", attempt + 1, e, wait_time)
        time.sleep(wait_time)
    else:
        log.error("All %d attempts failed: %s", MAX_RETRIES, e)
        raise e


def parse_ical_datetime(dt_value) -> tuple[Any, str | None]:
    if dt_value is None:
        return None, None
    dt_obj = dt_value.dt if hasattr(dt_value, "dt") else dt_value
    iso_str = dt_obj.isoformat() if isinstance(dt_obj, (datetime, date)) else None
    return dt_obj, iso_str


def map_priority(ical_priority: str | None) -> str:
    if ical_priority is None:
        return "Nessuna"
    try:
        p = int(ical_priority)
    except (ValueError, TypeError):
        return "Nessuna"
    if p == 0:
        return "Nessuna"
    if p <= 2:
        return "Alta"
    if p <= 5:
        return "Media"
    return "Bassa"


def map_status(ical_status: str | None) -> str:
    mapping = {
        "COMPLETED": "Completato",
        "IN-PROCESS": "In corso",
        "NEEDS-ACTION": "In corso",
    }
    return mapping.get((ical_status or "").upper(), "In corso")


def extract_rrule(vtodo_comp) -> str | None:
    if hasattr(vtodo_comp, "rrule"):
        return str(vtodo_comp.rrule.value)
    return None


def extract_last_modified(vtodo_comp) -> str | None:
    if hasattr(vtodo_comp, "last-modified"):
        return str(vtodo_comp.last_modified.value)
    if hasattr(vtodo_comp, "dtstamp"):
        return str(vtodo_comp.dtstamp.value)
    return datetime.now().isoformat()


def next_rrule_occurrence(rrule_str: str, from_date_iso: str | None) -> str | None:
    """Calcola la prossima occorrenza dell'RRULE dopo from_date_iso (ISO string).

    Usato per avanzare il DUE di un task ricorrente al completamento, senza mai
    scrivere STATUS:COMPLETED su CalDAV (evita di uccidere la serie su Synology).
    Ritorna una ISO date string (YYYY-MM-DD) o None se non calcolabile.
    """
    try:
        if from_date_iso:
            # Rimuovi eventuale componente ora (es. "2024-03-15T00:00:00")
            from_date = date.fromisoformat(from_date_iso[:10])
        else:
            from_date = date.today()

        dtstart = datetime(from_date.year, from_date.month, from_date.day)
        rule = rrulestr(rrule_str, dtstart=dtstart, ignoretz=True)
        nxt = rule.after(dtstart)
        if nxt:
            return nxt.date().isoformat()
    except Exception as exc:
        log.warning("next_rrule_occurrence failed (rrule=%r, from=%r): %s", rrule_str, from_date_iso, exc)
    return None


def parse_vtodo(vtodo_comp, list_name: str) -> dict[str, Any]:
    uid = str(vtodo_comp.uid.value) if hasattr(vtodo_comp, "uid") else None
    summary = str(vtodo_comp.summary.value) if hasattr(vtodo_comp, "summary") else ""
    description = str(vtodo_comp.description.value) if hasattr(vtodo_comp, "description") else ""

    _, due = parse_ical_datetime(vtodo_comp.due.value if hasattr(vtodo_comp, "due") else None)

    priority_raw = str(vtodo_comp.priority.value) if hasattr(vtodo_comp, "priority") else None
    priority = map_priority(priority_raw)

    location = str(vtodo_comp.location.value) if hasattr(vtodo_comp, "location") else ""
    url = str(vtodo_comp.url.value) if hasattr(vtodo_comp, "url") else ""

    status_raw = str(vtodo_comp.status.value) if hasattr(vtodo_comp, "status") else None
    status = map_status(status_raw)

    rrule = extract_rrule(vtodo_comp)
    last_modified = extract_last_modified(vtodo_comp)

    return {
        "uid": uid,
        "summary": summary,
        "description": description,
        "due": due,
        "priority": priority,
        "location": location,
        "url": url,
        "status": status,
        "rrule": rrule,
        "list_name": list_name,
        "last_modified": last_modified,
    }


def build_notion_properties(data: dict[str, Any]) -> dict:
    props = {}

    if data.get("summary"):
        props["Name"] = {"title": [{"text": {"content": data["summary"]}}]}
    else:
        props["Name"] = {"title": [{"text": {"content": "(senza titolo)"}}]}

    props["UID CalDAV"] = {"rich_text": [{"text": {"content": data.get("uid", "")}}]}

    if data.get("description"):
        desc = data["description"][:1990]
        props["Descrizione"] = {"rich_text": [{"text": {"content": desc}}]}

    if data.get("due"):
        props["Scadenza"] = {"date": {"start": data["due"]}}

    if data.get("priority"):
        props["Priorità"] = {"select": {"name": data["priority"]}}

    if data.get("location"):
        props["Luogo"] = {"rich_text": [{"text": {"content": data["location"]}}]}

    if data.get("url"):
        props["URL"] = {"url": data["url"]}

    if data.get("list_name"):
        props["Lista"] = {"select": {"name": data["list_name"]}}

    if data.get("rrule"):
        props["Periodicità"] = {"rich_text": [{"text": {"content": data["rrule"]}}]}

    if data.get("last_modified"):
        props["Ultima sync"] = {"date": {"start": data["last_modified"]}}

    # Status "Completato": la property Notion è di tipo Status (Done/Not started).
    # I task ricorrenti vengono sempre mostrati come Not started (il CalDAV server
    # gestisce il ciclo di vita dell'istanza).
    is_done = data.get("status") == "Completato" or data.get("completato", False)
    props["Completato"] = {"status": {"name": "Done" if is_done else "Not started"}}

    return props


def find_notion_page_by_uid(notion: Client, database_id: str, uid: str) -> str | None:
    try:
        response = notion.databases.query(
            database_id=database_id,
            filter={
                "property": "UID CalDAV",
                "rich_text": {"equals": uid},
            },
        )
        results = response.get("results", [])
        if results:
            return results[0]["id"]
    except Exception as e:
        log.error("Error finding Notion page by UID %s: %s", uid, e)
    return None


def get_notion_page_modified(notion: Client, page_id: str) -> str | None:
    try:
        page = notion.pages.retrieve(page_id=page_id)
        return page.get("last_edited_time")
    except Exception as e:
        log.error("Error getting Notion page %s: %s", page_id, e)
    return None


def fetch_all_caldav_todos(client) -> list[tuple[str, list]]:
    principal = client.principal()
    all_calendars = principal.calendars()
    log.info("=" * 60)
    log.info("[CalDAV] Total CalDAV collections found: %d", len(all_calendars))

    vtodo_lists = []
    for cal in all_calendars:
        try:
            display_name = str(cal.name) if cal.name else cal.url.split("/")[-2]
            log.info("[CalDAV] Checking collection: '%s' (url: %s)", display_name, cal.url)
            todos = cal.todos(include_completed=True)
            if todos:
                vtodo_lists.append((display_name, todos))
                log.info("[CalDAV] ✓ List '%s': %d VTODO item(s) found", display_name, len(todos))
            else:
                log.info("[CalDAV] ○ List '%s': 0 VTODO items", display_name)
        except Exception as exc:
            log.warning("[CalDAV] ✗ Could not read collection %s: %s", cal.url, exc)

    log.info("[CalDAV] Total VTODO lists with items: %d", len(vtodo_lists))
    log.info("=" * 60)
    return vtodo_lists


def fetch_all_notion_pages(notion: Client, database_id: str) -> list[dict]:
    all_pages = []
    has_more = True
    start_cursor = None
    
    while has_more:
        try:
            params = {"database_id": database_id}
            if start_cursor:
                params["start_cursor"] = start_cursor
            
            response = notion.databases.query(**params)
            all_pages.extend(response.get("results", []))
            has_more = response.get("has_more", False)
            start_cursor = response.get("next_cursor")
        except Exception as e:
            log.error("Error fetching Notion pages: %s", e)
            break
    
    return all_pages


def _get_rich_text(props: dict, key: str) -> str:
    prop = props.get(key)
    if prop and prop.get("rich_text"):
        return prop["rich_text"][0].get("text", {}).get("content", "")
    return ""


def _get_select(props: dict, key: str, default: str = "") -> str:
    prop = props.get(key)
    if prop and prop.get("select"):
        return prop["select"].get("name", default)
    return default


def parse_notion_page(page: dict) -> dict[str, Any]:
    props = page.get("properties", {})

    summary = ""
    if props.get("Name") and props["Name"].get("title"):
        summary = props["Name"]["title"][0].get("text", {}).get("content", "")

    due = None
    if props.get("Scadenza") and props["Scadenza"].get("date"):
        due = props["Scadenza"]["date"].get("start")

    completato = False
    if props.get("Completato"):
        # "Completato" è di tipo Status in Notion: Done = completato, altrimenti no
        status_name = props["Completato"].get("status", {}).get("name", "")
        completato = status_name == "Done"

    return {
        "page_id": page["id"],
        "uid": _get_rich_text(props, "UID CalDAV"),
        "summary": summary,
        "description": _get_rich_text(props, "Descrizione"),
        "due": due,
        "priority": _get_select(props, "Priorità", "Nessuna"),
        "location": _get_rich_text(props, "Luogo"),
        "url": (props.get("URL") or {}).get("url") or "",
        "list_name": _get_select(props, "Lista"),
        "rrule": _get_rich_text(props, "Periodicità"),
        "completato": completato,
        "last_modified": page.get("last_edited_time", ""),
    }


def reverse_priority(notion_priority: str) -> str:
    mapping = {
        "Alta": "1",
        "Media": "5",
        "Bassa": "9",
        "Nessuna": "0",
    }
    return mapping.get(notion_priority, "0")


def _ical_escape(value: str) -> str:
    """Escape special characters in iCal property values (RFC 5545)."""
    value = value.replace("\\", "\\\\")
    value = value.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
    value = value.replace(";", "\\;")
    return value


def build_ical_todo(data: dict[str, Any], existing_vtodo=None) -> str:
    uid = data.get("uid", "")
    summary = _ical_escape(data.get("summary", "(senza titolo)"))
    description = _ical_escape(data.get("description", ""))
    due = data.get("due")
    priority = reverse_priority(data.get("priority", "Nessuna"))
    location = data.get("location", "")
    url = data.get("url", "")
    rrule = data.get("rrule", "")

    # Checkbox "Completato" da Notion ha priorità sullo status testuale
    if data.get("completato"):
        status = "COMPLETED"
    elif data.get("status") == "Completato":
        status = "COMPLETED"
    else:
        status = "NEEDS-ACTION"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//vtodo-notion//EN",
        "BEGIN:VTODO",
        f"UID:{uid}",
        f"SUMMARY:{summary}",
        f"STATUS:{status}",
        f"PRIORITY:{priority}",
    ]

    if description:
        lines.append(f"DESCRIPTION:{description}")
    if due:
        # Notion può restituire date con ora e TZ (es. "2026-02-23T09:00:00.000+01:00").
        # iCal DUE accetta DATE (YYYYMMDD) o DATETIME (YYYYMMDDTHHMMSSZ).
        # Usiamo sempre solo la parte data per semplicità e compatibilità CalDAV.
        due_ical = due[:10].replace("-", "")  # "YYYY-MM-DD" → "YYYYMMDD"
        lines.append(f"DUE:{due_ical}")
    if location:
        lines.append(f"LOCATION:{location}")
    if url:
        lines.append(f"URL:{url}")
    if rrule:
        lines.append(f"RRULE:{rrule}")

    lines.extend([
        "END:VTODO",
        "END:VCALENDAR",
    ])

    return "\n".join(lines)


@with_retry
def upsert_notion_page(notion: Client, database_id: str, data: dict[str, Any], existing_page_id: str | None = None) -> bool:
    props = build_notion_properties(data)
    
    if existing_page_id:
        notion.pages.update(page_id=existing_page_id, properties=props)
        log.info("  → Updated Notion page for UID %s", data.get("uid"))
    else:
        notion.pages.create(
            parent={"database_id": database_id},
            properties=props,
        )
        log.info("  → Created Notion page for UID %s", data.get("uid"))
    
    return True


@with_retry
def update_caldav_todo(calendar, uid: str, ical_data: str) -> bool:
    try:
        results = calendar.search(todo=True, uid=uid)
        if results:
            todo_obj = results[0]
            todo_obj.data = ical_data
            todo_obj.save()
            log.info("  → Updated CalDAV todo %s", uid)
        else:
            calendar.add_todo(ical_data)
            log.info("  → Created CalDAV todo %s", uid)
        return True
    except Exception as e:
        log.error("  ✗ Error updating CalDAV todo %s: %s", uid, e)
        return False


def sync_caldav_to_notion(vtodo_lists: list, notion: Client, database_id: str, state: SyncState) -> dict:
    stats = {"created": 0, "updated": 0, "skipped": 0, "archived": 0, "errors": 0}

    # Pre-fetch all active Notion pages once to avoid per-item API calls (rate limit prevention)
    log.info("[CalDAV→Notion] Pre-fetching all Notion pages for UID lookup cache...")
    all_notion_pages = fetch_all_notion_pages(notion, database_id)
    notion_uid_map: dict[str, tuple[str, str]] = {}  # uid -> (page_id, last_edited_time)
    for page in all_notion_pages:
        uid_prop = page.get("properties", {}).get("UID CalDAV", {}).get("rich_text", [])
        if uid_prop:
            page_uid = uid_prop[0].get("text", {}).get("content", "")
            if page_uid:
                notion_uid_map[page_uid] = (page["id"], page.get("last_edited_time", ""))
    log.info("[CalDAV→Notion] Loaded %d Notion pages into UID lookup cache", len(notion_uid_map))

    total_caldav_todos = sum(len(todos) for _, todos in vtodo_lists)
    log.info("[CalDAV→Notion] === STARTING === Total VTODO items from CalDAV: %d", total_caldav_todos)

    for list_name, todos in vtodo_lists:
        log.info("[CalDAV→Notion] Processing list '%s' with %d items", list_name, len(todos))

        for idx, todo in enumerate(todos):
            try:
                vobj = todo.vobject_instance
                vtodo_comp = vobj.vtodo

                data = parse_vtodo(vtodo_comp, list_name)
                uid = data.get("uid")

                log.debug("[CalDAV→Notion] [%s:%d/%d] UID=%s summary='%s' status='%s' rrule=%s due=%s",
                          list_name, idx + 1, len(todos), uid,
                          (data.get("summary") or "")[:50], data.get("status"),
                          data.get("rrule") or "", data.get("due") or "")

                if not uid:
                    log.warning("[CalDAV→Notion] ⚠ Skipping VTODO without UID at index %d in list '%s'", idx, list_name)
                    stats["errors"] += 1
                    continue

                caldav_modified = data.get("last_modified", "")
                state.caldav_modified[uid] = caldav_modified

                is_completed = data.get("status") == "Completato"
                has_rrule = bool(data.get("rrule"))

                notion_entry = notion_uid_map.get(uid)
                existing_page_id = notion_entry[0] if notion_entry else None
                notion_modified = notion_entry[1] if notion_entry else None

                log.debug("[CalDAV→Notion] UID=%s: existing_page_id=%s, notion_modified='%s', caldav_modified='%s'",
                          uid, existing_page_id, notion_modified, caldav_modified)

                # Task completato su CalDAV senza ricorrenza: archivia in Notion (non cancella)
                if is_completed and not has_rrule:
                    if existing_page_id:
                        try:
                            notion.pages.update(page_id=existing_page_id, archived=True)
                            log.info("[CalDAV→Notion] ✓ ARCHIVED completed one-shot in Notion: UID=%s", uid)
                            stats["archived"] += 1
                        except Exception as e:
                            log.error("[CalDAV→Notion] ✗ Error archiving Notion page %s: %s", uid, e)
                            stats["errors"] += 1
                    else:
                        log.info("[CalDAV→Notion] ○ Completed one-shot not in Notion (skip): UID=%s", uid)
                    continue

                # Task ricorrente con STATUS:COMPLETED: Synology potrebbe essere in transizione
                # tra un'istanza e la successiva. Lo mostriamo sempre come attivo in Notion
                # e aggiorniamo la Scadenza con il nuovo DUE che il server ha già impostato.
                if is_completed and has_rrule:
                    data["status"] = "In corso"
                    data["completato"] = False
                    log.info("[CalDAV→Notion] ↻ Recurring COMPLETED on CalDAV (keep active in Notion): UID=%s", uid)

                if existing_page_id and notion_modified and caldav_modified <= notion_modified:
                    log.info("[CalDAV→Notion] ○ SKIPPED (Notion more recent): UID=%s", uid)
                    stats["skipped"] += 1
                    continue

                if upsert_notion_page(notion, database_id, data, existing_page_id):
                    if existing_page_id:
                        stats["updated"] += 1
                        log.info("[CalDAV→Notion] → UPDATED Notion page: UID=%s", uid)
                    else:
                        stats["created"] += 1
                        log.info("[CalDAV→Notion] → CREATED Notion page: UID=%s", uid)
                else:
                    stats["errors"] += 1

            except Exception as e:
                log.error("[CalDAV→Notion] ✗ ERROR at index %d in list '%s': %s", idx, list_name, e)
                stats["errors"] += 1

    return stats


def find_calendar_by_name(calendars: list, name: str):
    for cal in calendars:
        if str(cal.name) == name:
            return cal
    return None


def sync_notion_to_caldav(notion: Client, database_id: str, calendars: list, state: SyncState) -> dict:
    stats = {"updated": 0, "skipped": 0, "archived": 0, "recurring_completed": 0, "errors": 0}
    log.info("=" * 60)
    log.info("[SYNC] Starting Notion → CalDAV sync")
    log.info("=" * 60)

    pages = fetch_all_notion_pages(notion, database_id)
    log.info("[SYNC] Fetched %d pages from Notion database", len(pages))
    log.info("[SYNC] CalDAV calendars available: %s", [str(c.name) for c in calendars])

    for idx, page in enumerate(pages):
        try:
            data = parse_notion_page(page)
            uid = data.get("uid")
            page_id = data.get("page_id")

            log.debug("[Notion→CalDAV] [%d/%d] page_id=%s uid=%s summary='%s'",
                     idx + 1, len(pages), page_id, uid, data.get("summary", "")[:50])

            if not uid:
                log.warning("[Notion→CalDAV] ⚠ Skipping page without UID: page_id=%s", page_id)
                continue

            notion_modified = data.get("last_modified", "")
            state.notion_modified[uid] = notion_modified

            caldav_modified = state.caldav_modified.get(uid, "")

            has_rrule = bool(data.get("rrule"))
            completato = data.get("completato", False)

            log.debug("[Notion→CalDAV] UID=%s: completato=%s has_rrule=%s", uid, completato, has_rrule)

            # ── Gestione "Completato" spuntato da Notion ──────────────────────────────
            if completato:
                calendar = find_calendar_by_name(calendars, data.get("list_name"))

                if has_rrule:
                    # Task ricorrente: NON scriviamo STATUS:COMPLETED su CalDAV.
                    # Scrivere COMPLETED su un VTODO ricorrente potrebbe uccidere la serie
                    # permanentemente su Synology (comportamento non documentato).
                    # Approccio sicuro: avanziamo DUE alla prossima occorrenza con NEEDS-ACTION,
                    # esattamente come fanno Tasks.org e i client CalDAV robusti.
                    next_due = next_rrule_occurrence(data.get("rrule"), data.get("due"))
                    if next_due:
                        log.info("  ↻ Recurring task: advancing DUE %s → %s for %s",
                                 data.get("due"), next_due, uid)
                    else:
                        # RRULE esaurita o non calcolabile: avanziamo comunque il DUE di 1 giorno
                        # per evitare di bloccare il task. Il log avvisa l'utente.
                        fallback = date.today() + timedelta(days=1)
                        next_due = fallback.isoformat()
                        log.warning("  ⚠ Could not compute next RRULE occurrence for %s, "
                                    "advancing DUE by 1 day to %s", uid, next_due)

                    advanced_data = {**data, "due": next_due, "status": "In corso", "completato": False}
                    if calendar:
                        ical_data = build_ical_todo(advanced_data)
                        update_caldav_todo(calendar, uid, ical_data)

                    # Aggiorna Scadenza e resetta "Completato" a Not started in Notion
                    notion_props: dict = {"Completato": {"status": {"name": "Not started"}}}
                    notion_props["Scadenza"] = {"date": {"start": next_due}}
                    try:
                        notion.pages.update(page_id=page_id, properties=notion_props)
                        log.info("  ↻ Recurring task advanced to %s, checkbox reset in Notion: %s",
                                 next_due, uid)
                    except Exception as e:
                        log.error("  ✗ Error updating Notion page for recurring task %s: %s", uid, e)
                    stats["recurring_completed"] += 1
                else:
                    # Task one-shot: scrive COMPLETED su CalDAV e archivia la pagina Notion
                    if calendar:
                        ical_data = build_ical_todo(data)
                        update_caldav_todo(calendar, uid, ical_data)
                    try:
                        notion.pages.update(page_id=page_id, archived=True)
                        log.info("  ✓ One-shot task completed and archived in Notion: %s", uid)
                    except Exception as e:
                        log.error("  ✗ Error archiving Notion page %s: %s", uid, e)
                    stats["archived"] += 1
                continue

            # ── Flusso normale: sincronizza modifiche da Notion → CalDAV ─────────────
            if caldav_modified and notion_modified <= caldav_modified:
                log.info("  ○ Skipped (CalDAV more recent): %s", uid)
                stats["skipped"] += 1
                continue

            calendar = find_calendar_by_name(calendars, data.get("list_name"))

            if not calendar:
                log.warning("[Notion→CalDAV] ⚠ Calendar not found for list '%s', skipping UID=%s",
                           data.get("list_name"), uid)
                stats["skipped"] += 1
                continue

            ical_data = build_ical_todo(data)

            if update_caldav_todo(calendar, uid, ical_data):
                stats["updated"] += 1
                log.info("[Notion→CalDAV] → Updated CalDAV: UID=%s", uid)
            else:
                stats["errors"] += 1

        except Exception as e:
            log.error("[Notion→CalDAV] ✗ Error at index %d: %s", idx, e)
            stats["errors"] += 1

    log.info("[Notion→CalDAV] Complete: updated=%d, skipped=%d, archived=%d, recurring_completed=%d, errors=%d",
             stats["updated"], stats["skipped"], stats["archived"], stats["recurring_completed"], stats["errors"])
    log.info("=" * 60)
    return stats


def sync():
    global circuit_breaker_triggered
    
    log.info("=" * 60)
    log.info("Starting bidirectional sync CalDAV ↔ Notion")
    log.info("=" * 60)
    
    state = load_state()
    
    log.info("Connecting to CalDAV: %s", CALDAV_URL)
    client = caldav.DAVClient(
        url=CALDAV_URL,
        username=CALDAV_USERNAME,
        password=CALDAV_PASSWORD,
    )
    principal = client.principal()
    calendars = principal.calendars()
    
    log.info("Connecting to Notion...")
    notion = Client(auth=NOTION_TOKEN)
    
    vtodo_lists = fetch_all_caldav_todos(client)
    
    if not vtodo_lists:
        log.info("No CalDAV VTODO items found.")
    else:
        log.info("-" * 40)
        caldav_stats = sync_caldav_to_notion(vtodo_lists, notion, NOTION_DATABASE_ID, state)
        log.info(
            "CalDAV → Notion: created=%d, updated=%d, skipped=%d, archived=%d, errors=%d",
            caldav_stats["created"], caldav_stats["updated"],
            caldav_stats["skipped"], caldav_stats["archived"], caldav_stats["errors"],
        )

    # Reset circuit breaker tra le due direzioni: errori CalDAV→Notion non devono
    # bloccare Notion→CalDAV (sono pipeline indipendenti).
    global circuit_breaker_errors, circuit_breaker_triggered
    circuit_breaker_errors = 0
    circuit_breaker_triggered = False

    log.info("-" * 40)
    notion_stats = sync_notion_to_caldav(notion, NOTION_DATABASE_ID, calendars, state)
    log.info(
        "Notion → CalDAV: updated=%d, skipped=%d, archived=%d, recurring_completed=%d, errors=%d",
        notion_stats["updated"], notion_stats["skipped"],
        notion_stats["archived"], notion_stats["recurring_completed"], notion_stats["errors"],
    )

    state.last_sync = datetime.now().isoformat()
    save_state(state)

    # Notify if error rate is high (>20% of items failed)
    total_items = sum([
        caldav_stats.get("created", 0), caldav_stats.get("updated", 0),
        caldav_stats.get("skipped", 0), caldav_stats.get("archived", 0),
        caldav_stats.get("errors", 0),
    ]) if vtodo_lists else 0
    total_errors = (caldav_stats.get("errors", 0) if vtodo_lists else 0) + notion_stats.get("errors", 0)
    if total_errors > 0 and total_items > 0 and total_errors / total_items > 0.2:
        notify(
            "vtodo-notion: sync errors",
            f"Sync completata con {total_errors} errori su {total_items} elementi.\n"
            "Controlla i log del container vtodo-notion.",
        )

    if circuit_breaker_triggered:
        log.warning("Circuit breaker is active - next sync will resume normal operation")
        circuit_breaker_triggered = False
        notify(
            "vtodo-notion: circuit breaker attivato",
            "Troppi errori consecutivi — un ciclo di sync è stato saltato.\n"
            "Controlla i log del container vtodo-notion.",
        )

    log.info("=" * 60)
    log.info("Sync complete")
    log.info("=" * 60)


if __name__ == "__main__":
    try:
        sync()
    except Exception as exc:
        log.error("Fatal sync error: %s", exc)
        notify(
            "vtodo-notion: errore fatale",
            f"Il sync si è interrotto con un errore critico:\n`{exc}`\n"
            "Controlla i log del container vtodo-notion.",
        )
        raise
