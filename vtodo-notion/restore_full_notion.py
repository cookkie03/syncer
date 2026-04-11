
import os
import sys
import glob
import re
import json
from notion_client import Client

# Add shared to path
sys.path.insert(0, "/shared")
from config_loader import require_env

NOTION_TOKEN = require_env("NOTION_TOKEN")
DATABASE_ID = require_env("NOTION_DATABASE_ID")
BACKUP_DIR = "/data/backup" # Map to container volume

notion = Client(auth=NOTION_TOKEN)

def get_tasks_from_ics():
    tasks = []
    # All task files
    ics_files = glob.glob(f"{BACKUP_DIR}/tasks_*.ics")
    print(f"Found {len(ics_files)} ICS files.")
    for ics_path in ics_files:
        list_name = os.path.basename(ics_path).replace("tasks_", "").replace(".ics", "")
        with open(ics_path, "r") as f:
            content = f.read()
        
        blocks = re.findall(r"BEGIN:VTODO.*?END:VTODO", content, re.DOTALL)
        for block in blocks:
            uid_match = re.search(r"UID:(.*?)\s", block)
            summary_match = re.search(r"SUMMARY:(.*?)(?:\s[A-Z-]*:|\sEND:VTODO)", block, re.DOTALL)
            description_match = re.search(r"DESCRIPTION:(.*?)(?:\s[A-Z-]*:|\sEND:VTODO)", block, re.DOTALL)
            due_match = re.search(r"DUE;.*?VALUE=DATE:(.*?)\s", block)
            if not due_match:
                due_match = re.search(r"DUE:(.*?)\s", block)
            
            uid = uid_match.group(1).strip() if uid_match else None
            summary = summary_match.group(1).strip().replace("
 ", "") if summary_match else "(Senza titolo)"
            description = description_match.group(1).strip().replace("
 ", "") if description_match else ""
            due = due_match.group(1).strip() if due_match else None
            
            if uid:
                tasks.append({
                    "uid": uid,
                    "summary": summary,
                    "description": description,
                    "due": due,
                    "list_name": list_name
                })
    return tasks

def restore_notion(tasks):
    print(f"Total tasks to check: {len(tasks)}")
    
    # 1. Fetch active pages
    active_pages = {}
    has_more = True
    cursor = None
    while has_more:
        params = {"database_id": DATABASE_ID}
        if cursor:
            params["start_cursor"] = cursor
        resp = notion.databases.query(**params)
        for page in resp.get("results", []):
            uid_prop = page["properties"].get("UID CalDAV", {}).get("rich_text", [])
            if uid_prop:
                uid = uid_prop[0]["text"]["content"]
                active_pages[uid] = page["id"]
        has_more = resp.get("has_more", False)
        cursor = resp.get("next_cursor")
    
    print(f"Active pages in Notion: {len(active_pages)}")
    
    restored = 0
    created = 0
    
    for task in tasks:
        uid = task["uid"]
        if uid in active_pages:
            continue
        
        # Search by UID (searching only archived pages if possible, but search() is global)
        search_results = notion.search(query=uid, filter={"value": "page", "property": "object"}).get("results", [])
        found = False
        for page in search_results:
            uid_prop = page["properties"].get("UID CalDAV", {}).get("rich_text", [])
            if uid_prop and uid_prop[0]["text"]["content"] == uid:
                if page.get("archived"):
                    print(f"Unarchiving: {task['summary']} ({uid})")
                    notion.pages.update(page_id=page["id"], archived=False)
                    notion.pages.update(page_id=page["id"], properties={
                        "Completato": {"status": {"name": "In progress"}}
                    })
                    restored += 1
                found = True
                break
        
        if not found:
            print(f"Creating: {task['summary']} ({uid})")
            props = {
                "Name": {"title": [{"text": {"content": task["summary"]}}]},
                "UID CalDAV": {"rich_text": [{"text": {"content": uid}}]},
                "Completato": {"status": {"name": "In progress"}},
                "Descrizione": {"rich_text": [{"text": {"content": task["description"][:1900]}}]} if task["description"] else {"rich_text": []},
                "Lista": {"select": {"name": task["list_name"]}}
            }
            if task["due"]:
                d = task["due"]
                if len(d) >= 8:
                    iso_due = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                    props["Scadenza"] = {"date": {"start": iso_due}}
            
            notion.pages.create(parent={"database_id": DATABASE_ID}, properties=props)
            created += 1

    print(f"Done. Restored: {restored}, Created: {created}")

if __name__ == "__main__":
    tasks = get_tasks_from_ics()
    restore_notion(tasks)
