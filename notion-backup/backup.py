#!/usr/bin/env python3
"""
notion-backup — Notion workspace backup via Official API → structured JSON.

Uses the permanent integration token (NOTION_TOKEN) — no browser cookies needed.
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Config ─────────────────────────────────────────────────────────────────
for _p in ["/shared", str(Path(__file__).resolve().parent.parent / "shared")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
from config_loader import cfg, require_env, env  # noqa: E402

NOTION_TOKEN       = require_env("NOTION_TOKEN")
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = env("TELEGRAM_CHAT_ID")

BACKUP_DIR       = Path(cfg("notion_backup.backup_dir", "/backup"))
JSON_DIR         = BACKUP_DIR / "json"

NOTION_API_BASE  = "https://api.notion.com/v1"
NOTION_VERSION   = cfg("notion_backup.notion_api_version", "2022-06-28")
TELEGRAM_TIMEOUT = cfg("shared.telegram_timeout", 10, int)

RATE_LIMIT_RPS   = cfg("notion_backup.rate_limit_rps", 3, float)
MIN_REQ_INTERVAL = 1.0 / RATE_LIMIT_RPS
_last_req_time   = [0.0]

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("notion-backup")


# ════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ════════════════════════════════════════════════════════════════════════════

def notify(title: str, message: str) -> None:
    """Send a Telegram message. No-op if TELEGRAM_BOT_TOKEN/CHAT_ID are not set."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    text = f"🔔 *{title}*\n\n{message}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=TELEGRAM_TIMEOUT,
        )
    except Exception as exc:
        log.warning("[notify] Failed to send Telegram message: %s", exc)


def save_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unlink before writing: breaks any existing hardlink so older snapshots
    # retain their own inode (and thus their old content) undisturbed.
    path.unlink(missing_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def extract_title(obj: dict) -> str:
    """Return a human-readable title from a Notion page or database object."""
    kind = obj.get("object", "")
    if kind == "page":
        props = obj.get("properties", {})
        for key in ("title", "Title", "Name"):
            if key in props:
                rich = props[key].get("title", [])
                if rich:
                    return rich[0].get("plain_text", "")
        return "(untitled page)"
    if kind == "database":
        rich = obj.get("title", [])
        if rich:
            return rich[0].get("plain_text", "")
        return "(untitled database)"
    return "(unknown)"


# ════════════════════════════════════════════════════════════════════════════
# Track 1 — Official API → JSON
# ════════════════════════════════════════════════════════════════════════════

def _rate_limited(fn, *args, **kwargs):
    """Wrap an HTTP call with a rate limiter (3 req/s)."""
    now = time.monotonic()
    wait = MIN_REQ_INTERVAL - (now - _last_req_time[0])
    if wait > 0:
        time.sleep(wait)
    _last_req_time[0] = time.monotonic()
    return fn(*args, **kwargs)


def api_get(session: requests.Session, path: str, **kwargs) -> dict:
    resp = _rate_limited(session.get, f"{NOTION_API_BASE}{path}", **kwargs)
    resp.raise_for_status()
    return resp.json()


def api_post(session: requests.Session, path: str, **kwargs) -> dict:
    resp = _rate_limited(session.post, f"{NOTION_API_BASE}{path}", **kwargs)
    resp.raise_for_status()
    return resp.json()


def search_all(session: requests.Session, filter_value: str) -> list[dict]:
    """Return all objects from /v1/search for a given filter value (page/database)."""
    results, cursor = [], None
    while True:
        body: dict = {
            "filter": {"value": filter_value, "property": "object"},
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        data = api_post(session, "/search", json=body)
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def fetch_blocks(session: requests.Session, block_id: str) -> list[dict]:
    """Recursively fetch all block children for a page or block."""
    blocks, cursor = [], None
    while True:
        params: dict = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = api_get(session, f"/blocks/{block_id}/children", params=params)
        for block in data.get("results", []):
            if block.get("has_children"):
                block["_children"] = fetch_blocks(session, block["id"])
            blocks.append(block)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return blocks


def fetch_db_rows(session: requests.Session, db_id: str) -> list[dict]:
    """Fetch all rows from a database, following pagination."""
    rows, cursor = [], None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = api_post(session, f"/databases/{db_id}/query", json=body)
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return rows


def run_track1(session: requests.Session) -> tuple[int, int]:
    """
    Crawl entire workspace via official Notion API and persist as JSON.
    Returns (page_count, database_count).
    """
    log.info("[Track1] Starting structured JSON backup...")
    JSON_DIR.mkdir(parents=True, exist_ok=True)

    log.info("[Track1] Searching for all pages...")
    pages = search_all(session, "page")
    log.info("[Track1] Found %d pages", len(pages))

    log.info("[Track1] Searching for all databases...")
    databases = search_all(session, "database")
    log.info("[Track1] Found %d databases", len(databases))

    manifest_entries: list[dict] = []

    for obj in pages:
        oid   = obj["id"]
        title = extract_title(obj)
        try:
            save_json(JSON_DIR / oid / "content.json", obj)
            blocks = fetch_blocks(session, oid)
            save_json(JSON_DIR / oid / "blocks.json", blocks)
            log.info("[Track1] page       | %s | %s | %d blocks", oid, title, len(blocks))
            manifest_entries.append({"id": oid, "title": title, "type": "page", "block_count": len(blocks)})
        except Exception as exc:
            log.error("[Track1] Error on page %s (%s): %s", oid, title, exc)

    for obj in databases:
        oid   = obj["id"]
        title = extract_title(obj)
        try:
            save_json(JSON_DIR / oid / "content.json", obj)
            blocks = fetch_blocks(session, oid)
            save_json(JSON_DIR / oid / "blocks.json", blocks)
            rows = fetch_db_rows(session, oid)
            save_json(JSON_DIR / oid / "rows.json", rows)
            log.info("[Track1] database   | %s | %s | %d blocks | %d rows",
                     oid, title, len(blocks), len(rows))
            manifest_entries.append({
                "id": oid, "title": title, "type": "database",
                "block_count": len(blocks), "row_count": len(rows),
            })
        except Exception as exc:
            log.error("[Track1] Error on database %s (%s): %s", oid, title, exc)

    manifest = {
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "total_pages":      len(pages),
        "total_databases":  len(databases),
        "objects":          manifest_entries,
    }
    save_json(JSON_DIR / "manifest.json", manifest)
    log.info("[Track1] Done — %d pages, %d databases", len(pages), len(databases))
    return len(pages), len(databases)


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    log.info("=== notion-backup starting ===")

    session = requests.Session()
    session.headers.update({
        "Authorization":  f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type":   "application/json",
    })

    try:
        pages, dbs = run_track1(session)
        log.info("Backup complete — %d pages, %d databases", pages, dbs)
    except Exception as exc:
        log.error("Backup failed: %s", exc)
        notify(
            "Notion backup: FAILED",
            "JSON backup via official API did not complete. Check container logs.",
        )

    log.info("=== notion-backup done ===")


if __name__ == "__main__":
    main()
