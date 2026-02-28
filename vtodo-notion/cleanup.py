#!/usr/bin/env python3
"""
CalDAV + Notion cleanup script.

Operazioni:
1. Elimina VTODO completati RICORRENTI da CalDAV (copie fantasma post-completamento)
2. Elimina la pagina Notion fantasma con UID suffisso numerico
3. Verifica il duplicato UID EF056B36

NON tocca i completati non-ricorrenti (quelli sono storia legittima).
"""

import os
import sys
import re
import logging
import caldav
from datetime import date, timedelta
from notion_client import Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("cleanup")

CALDAV_URL      = os.environ.get("CALDAV_URL", "")
CALDAV_USERNAME = os.environ.get("CALDAV_USERNAME", "")
CALDAV_PASSWORD = os.environ.get("CALDAV_PASSWORD", "")
NOTION_TOKEN    = os.environ.get("NOTION_TOKEN", "")
NOTION_DB_ID    = os.environ.get("NOTION_DATABASE_ID", "")

DRY_RUN = "--apply" not in sys.argv


def main():
    if DRY_RUN:
        log.info("=== DRY RUN === (usa --apply per eseguire davvero)")
    else:
        log.info("=== APPLY MODE === Le modifiche saranno effettive!")

    # ── 1. Connessione CalDAV ──────────────────────────────────────────────
    log.info("Connecting to CalDAV: %s", CALDAV_URL)
    client = caldav.DAVClient(
        url=CALDAV_URL,
        username=CALDAV_USERNAME,
        password=CALDAV_PASSWORD,
    )
    principal = client.principal()
    calendars = principal.calendars()
    log.info("Found %d CalDAV collections", len(calendars))

    # ── 2. Elimina VTODO completati RICORRENTI ───────────────────────────
    # Quando Synology completa un'occorrenza ricorrente, crea una nuova
    # istanza per la prossima occorrenza e tiene la vecchia come COMPLETED.
    # Queste copie fantasma confondono il sync e vanno eliminate.
    total_deleted = 0
    total_skipped = 0

    for cal in calendars:
        name = str(cal.name) if cal.name else "?"
        try:
            todos = cal.todos(include_completed=True)
        except Exception as e:
            log.warning("Could not read %s: %s", name, e)
            continue

        to_delete = []
        for todo in todos:
            try:
                vobj = todo.vobject_instance
                vtodo = vobj.vtodo

                uid = str(vtodo.uid.value) if hasattr(vtodo, "uid") else ""
                status = str(vtodo.status.value) if hasattr(vtodo, "status") else ""
                has_rrule = hasattr(vtodo, "rrule")
                summary = str(vtodo.summary.value)[:50] if hasattr(vtodo, "summary") else ""

                if status == "COMPLETED" and has_rrule:
                    to_delete.append((todo, uid, summary))
            except Exception as e:
                log.warning("Parse error in %s: %s", name, e)

        if to_delete:
            log.info("[%s] %d completed recurring to delete (of %d total)",
                     name, len(to_delete), len(todos))
            for todo_obj, uid, summary in to_delete:
                if DRY_RUN:
                    log.info("  [DRY] Would delete: %s (%s)", summary, uid[:30])
                else:
                    try:
                        todo_obj.delete()
                        log.info("  DELETED: %s (%s)", summary, uid[:30])
                        total_deleted += 1
                    except Exception as e:
                        log.error("  FAILED to delete %s: %s", uid[:30], e)
                        total_skipped += 1
        else:
            log.info("[%s] No completed recurring to clean (%d items)", name, len(todos))

    log.info("CalDAV cleanup: deleted=%d, skipped=%d", total_deleted, total_skipped)

    # ── 3. Verifica duplicato UID EF056B36 ────────────────────────────────
    dupe_uid = "EF056B36-1C0B-4D69-BB81-95AE2353803A"
    log.info("Checking duplicate UID %s...", dupe_uid)
    found_count = 0
    for cal in calendars:
        try:
            results = cal.search(todo=True, uid=dupe_uid)
            for r in results:
                vobj = r.vobject_instance
                vtodo = vobj.vtodo
                status = str(vtodo.status.value) if hasattr(vtodo, "status") else ""
                summary = str(vtodo.summary.value)[:50] if hasattr(vtodo, "summary") else ""
                cal_name = str(cal.name) if cal.name else "?"
                log.info("  Found in [%s]: %s status=%s", cal_name, summary, status)
                found_count += 1
        except Exception:
            pass
    if found_count <= 1:
        log.info("  Duplicate already resolved (only %d instance left)", found_count)
    else:
        log.warning("  Still %d instances! Manual intervention needed.", found_count)

    # ── 4. Elimina pagina Notion fantasma con UID suffisso ────────────────
    log.info("Connecting to Notion...")
    notion = Client(auth=NOTION_TOKEN)

    phantom_uid = "c1b44b50-6933-4f50-864e-359cd6d9e726-1762354080"
    log.info("Searching for phantom Notion page with UID: %s", phantom_uid)

    try:
        response = notion.databases.query(
            database_id=NOTION_DB_ID,
            filter={
                "property": "UID CalDAV",
                "rich_text": {"equals": phantom_uid},
            },
        )
        results = response.get("results", [])
        if results:
            page_id = results[0]["id"]
            title_parts = results[0].get("properties", {}).get("Name", {}).get("title", [])
            title = title_parts[0].get("text", {}).get("content", "") if title_parts else "?"
            log.info("  Found phantom page: '%s' (page_id=%s)", title, page_id)
            if DRY_RUN:
                log.info("  [DRY] Would archive this page")
            else:
                notion.pages.update(page_id=page_id, archived=True)
                log.info("  ARCHIVED phantom page")
        else:
            log.info("  No phantom page found (already clean)")
    except Exception as e:
        log.error("  Error searching/archiving phantom page: %s", e)

    # ── 5. Cerca altre pagine Notion con UID suffisso numerico ────────────
    log.info("Scanning Notion for other pages with numeric-suffix UIDs...")
    all_pages = []
    has_more = True
    cursor = None
    while has_more:
        params = {"database_id": NOTION_DB_ID}
        if cursor:
            params["start_cursor"] = cursor
        resp = notion.databases.query(**params)
        all_pages.extend(resp.get("results", []))
        has_more = resp.get("has_more", False)
        cursor = resp.get("next_cursor")

    phantom_count = 0
    for page in all_pages:
        uid_prop = page.get("properties", {}).get("UID CalDAV", {}).get("rich_text", [])
        if uid_prop:
            uid = uid_prop[0].get("text", {}).get("content", "")
            if re.search(r"-\d{8,}$", uid):
                title_parts = page.get("properties", {}).get("Name", {}).get("title", [])
                title = title_parts[0].get("text", {}).get("content", "") if title_parts else "?"
                log.info("  Phantom UID: %s ('%s')", uid, title)
                phantom_count += 1
                if not DRY_RUN:
                    try:
                        notion.pages.update(page_id=page["id"], archived=True)
                        log.info("    ARCHIVED")
                    except Exception as e:
                        log.error("    FAILED: %s", e)

    if phantom_count == 0:
        log.info("  No phantom pages found")
    else:
        log.info("  Total phantom pages: %d", phantom_count)

    # ── Summary ──────────────────────────────────────────────────────────
    log.info("=" * 60)
    if DRY_RUN:
        log.info("DRY RUN complete. Run with --apply to execute.")
    else:
        log.info("Cleanup complete!")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
