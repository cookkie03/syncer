"""
Restore tasks to CalDAV from Notion.
Reads UIDs from restore_tasks.json, queries Notion for full details,
and creates VTODO entries on CalDAV with the same UIDs.
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone

# Add shared and current directory to path
for _p in ["/shared", "/app/shared", str(Path(__file__).resolve().parent.parent / "shared")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from config_loader import cfg, require_env
    CALDAV_URL = require_env("CALDAV_URL")
    CALDAV_USERNAME = require_env("CALDAV_USERNAME")
    CALDAV_PASSWORD = require_env("CALDAV_PASSWORD")
    NOTION_TOKEN = require_env("NOTION_TOKEN")
    NOTION_DATABASE_ID = require_env("NOTION_DATABASE_ID")
except ImportError:
    print("[ERROR] config_loader not found. Run inside Docker container.")
    sys.exit(1)

import caldav
from notion_client import Client


def ical_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n").replace(";", "\\;")


def build_ical(uid: str, summary: str, description: str = "", due: str = None, 
               priority: str = "Nessuna", is_completed: bool = False, 
               location: str = "", url: str = "", rrule: str = "", 
               list_name: str = "Tasks") -> str:
    """Build iCalendar VTODO string."""
    summary = ical_escape(summary or "(senza titolo)")
    desc = ical_escape(description or "")
    PRIORITY_MAP = {"Urgenze": "1", "Alta": "3", "Media": "5", "Nessuna": "0"}
    priority_val = PRIORITY_MAP.get(priority, "0")
    status = "COMPLETED" if is_completed else "NEEDS-ACTION"
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//vtodo-notion//EN",
        "BEGIN:VTODO",
        f"UID:{uid}", f"DTSTAMP:{now}", f"LAST-MODIFIED:{now}",
        f"SUMMARY:{summary}", f"STATUS:{status}", f"PRIORITY:{priority_val}",
    ]
    if desc:
        lines.append(f"DESCRIPTION:{desc}")
    if due:
        if "T" in due:
            try:
                dt = datetime.fromisoformat(due)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt_utc = dt.astimezone(timezone.utc)
                lines.append(f"DUE:{dt_utc.strftime('%Y%m%dT%H%M%SZ')}")
            except (ValueError, TypeError):
                lines.append(f"DUE:{due[:10].replace('-', '')}")
        else:
            lines.append(f"DUE:{due[:10].replace('-', '')}")
    if location:
        lines.append(f"LOCATION:{ical_escape(location)}")
    if url:
        lines.append(f"URL:{url}")
    if rrule:
        lines.append(f"RRULE:{rrule}")
    lines.extend(["END:VTODO", "END:VCALENDAR"])
    return "\n".join(lines)


def get_rt(props: dict, key: str) -> str:
    p = props.get(key)
    if p and p.get("rich_text"):
        return p["rich_text"][0].get("text", {}).get("content", "")
    return ""


def get_sel(props: dict, key: str, default: str = "") -> str:
    p = props.get(key)
    if p and p.get("select"):
        return p["select"].get("name", default)
    return default


def query_notion_task(notion: Client, database_id: str, uid: str) -> dict | None:
    """Query Notion for a specific task by UID CalDAV."""
    try:
        resp = notion.databases.query(
            database_id=database_id,
            filter={
                "property": "UID CalDAV",
                "rich_text": {"equals": uid}
            }
        )
        results = resp.get("results", [])
        if results:
            return results[0]
        return None
    except Exception as e:
        print(f"[ERROR] Query Notion failed for {uid[:20]}: {e}")
        return None


def main():
    # Load UIDs from restore_tasks.json
    restore_file = Path(__file__).resolve().parent / "state" / "restore_tasks.json"
    if not restore_file.exists():
        restore_file = Path("/app/state/restore_tasks.json")
    if not restore_file.exists():
        restore_file = Path("/data/restore_tasks.json")
    
    with open(restore_file) as f:
        tasks_to_restore = json.load(f)
    
    print(f"[INFO] Loaded {len(tasks_to_restore)} tasks to restore")
    
    # Connect to Notion
    notion = Client(auth=NOTION_TOKEN)
    
    # Connect to CalDAV
    print("[INFO] Connecting to CalDAV...")
    client = caldav.DAVClient(
        url=CALDAV_URL,
        username=CALDAV_USERNAME,
        password=CALDAV_PASSWORD,
    )
    principal = client.principal()
    calendars = list(principal.calendars())
    
    if not calendars:
        print("[ERROR] No calendars found on CalDAV")
        sys.exit(1)
    
    # Find the Tasks calendar
    target_cal = None
    for cal in calendars:
        name = cal.get_display_name() or ""
        if "Tasks" in name or "tasks" in name.lower():
            target_cal = cal
            print(f"[INFO] Using calendar: {name}")
            break
    
    if not target_cal:
        target_cal = calendars[0]
        print(f"[INFO] Using default calendar: {target_cal.get_display_name()}")
    
    # Process each task
    restored = 0
    errors = 0
    
    for task_info in tasks_to_restore:
        uid = task_info["uid"]
        title = task_info["title"]
        
        print(f"\n[RESTORE] UID: {uid[:20]}... Title: {title[:50]}...")
        
        # Query Notion for full task details
        page = query_notion_task(notion, NOTION_DATABASE_ID, uid)
        
        if page:
            props = page.get("properties", {})
            
            summary = ""
            if props.get("Name") and props["Name"].get("title"):
                summary = props["Name"]["title"][0].get("text", {}).get("content", "")
            
            description = get_rt(props, "Descrizione")
            due = None
            if props.get("Scadenza") and props["Scadenza"].get("date"):
                due = props["Scadenza"]["date"].get("start", "")
            
            priority = get_sel(props, "Priorità", "Nessuna")
            location = get_rt(props, "Luogo")
            url = (props.get("URL") or {}).get("url") or ""
            rrule = get_rt(props, "Periodicità")
            list_name = get_sel(props, "Lista", "Tasks")
            
            is_completed = False
            if props.get("Completato"):
                status_name = props["Completato"].get("status", {}).get("name", "Not started")
                is_completed = status_name == "Done"
        else:
            # Task not found in Notion - use minimal info
            print(f"  [WARN] Task not found in Notion, using saved title only")
            summary = title
            description = ""
            due = None
            priority = "Nessuna"
            location = ""
            url = ""
            rrule = ""
            list_name = "Tasks"
            is_completed = False
        
        # Build iCalendar
        ical = build_ical(
            uid=uid,
            summary=summary,
            description=description,
            due=due,
            priority=priority,
            is_completed=is_completed,
            location=location,
            url=url,
            rrule=rrule,
            list_name=list_name
        )
        
        # Check if already exists
        try:
            existing = target_cal.search(todo=True, uid=uid)
            if existing:
                print(f"  [SKIP] Task already exists on CalDAV")
                continue
        except Exception as e:
            print(f"  [WARN] Search failed: {e}")
        
        # Create on CalDAV
        try:
            target_cal.add_todo(ical)
            print(f"  [OK] Created on CalDAV")
            restored += 1
        except Exception as e:
            print(f"  [ERROR] Failed to create: {e}")
            errors += 1
    
    print(f"\n[DONE] Restored: {restored}, Errors: {errors}")


if __name__ == "__main__":
    main()