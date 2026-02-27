#!/usr/bin/env python3
"""
notion-backup — dual-track Notion workspace backup

Track 1: Official API (Bearer token) → structured JSON
Track 2: Internal export API (token_v2 cookie) → Markdown ZIP

Both tracks run concurrently. A failure in one does NOT stop the other.
Git versioning runs after both complete.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("notion-backup")


# ── Environment ────────────────────────────────────────────────────────────
def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        log.error("Required environment variable %s is not set", name)
        sys.exit(1)
    return value


NOTION_API_TOKEN = require_env("NOTION_API_TOKEN")
NOTION_TOKEN_V2   = os.environ.get("NOTION_TOKEN_V2", "")
NOTION_FILE_TOKEN = os.environ.get("NOTION_FILE_TOKEN", "")
NOTION_SPACE_ID   = os.environ.get("NOTION_SPACE_ID", "")
GIT_REMOTE_URL        = os.environ.get("GIT_REMOTE_URL", "")
TELEGRAM_BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")

BACKUP_DIR        = Path("/backup")
JSON_DIR          = BACKUP_DIR / "json"
MD_LATEST_DIR     = BACKUP_DIR / "html" / "latest"
MD_ARCHIVES_DIR   = BACKUP_DIR / "html" / "archives"
SNAP_DAILY_DIR    = BACKUP_DIR / "snapshots" / "daily"
SNAP_WEEKLY_DIR   = BACKUP_DIR / "snapshots" / "weekly"

KEEP_DAILY   = 7   # days
KEEP_WEEKLY  = 8   # weeks

NOTION_API_BASE  = "https://api.notion.com/v1"
NOTION_VERSION   = "2022-06-28"
EXPORT_BASE      = "https://www.notion.so/api/v3"

# Track 2 polling settings
POLL_INTERVAL_S  = 10
POLL_TIMEOUT_S   = 30 * 60  # 30 minutes

# Track 1 rate limit: max 3 req/s → min 333 ms between requests
MIN_REQ_INTERVAL = 1.0 / 3
_rate_lock       = threading.Lock()
_last_req_time   = [0.0]  # list so it's mutable inside nested functions


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
            timeout=10,
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
    """Wrap an HTTP call with a token-bucket rate limiter (3 req/s)."""
    with _rate_lock:
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
# Track 2 — Native export via internal API
# ════════════════════════════════════════════════════════════════════════════

def run_track2() -> bool:
    """
    Trigger native Notion workspace export (Markdown ZIP) via internal API.
    Returns True on success, False on any failure (does NOT raise).
    """
    if not all([NOTION_TOKEN_V2, NOTION_SPACE_ID]):
        log.warning(
            "[Track2] Skipping — NOTION_TOKEN_V2 and/or NOTION_SPACE_ID is not set. "
            "Set them to enable native Markdown/HTML export."
        )
        return False

    log.info("[Track2] Starting native HTML export...")
    if not NOTION_FILE_TOKEN:
        log.info("[Track2] NOTION_FILE_TOKEN not set — attempting export with token_v2 only.")

    session = requests.Session()
    cookies = {"token_v2": NOTION_TOKEN_V2}
    if NOTION_FILE_TOKEN:
        cookies["file_token"] = NOTION_FILE_TOKEN
    session.cookies.update(cookies)
    session.headers.update({"Content-Type": "application/json"})

    # ── Enqueue export task ─────────────────────────────────────────────────
    task_payload = {
        "task": {
            "eventName": "exportSpace",
            "request": {
                "spaceId": NOTION_SPACE_ID,
                "exportOptions": {
                    "exportType":      "html",
                    "timeZone":        "Europe/Rome",
                    "locale":          "en",
                    "includeContents": "no_files",
                },
            },
        }
    }
    try:
        resp = session.post(f"{EXPORT_BASE}/enqueueTask", json=task_payload, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("[Track2] Failed to enqueue export task: %s", exc)
        log.warning(
            "[Track2] POSSIBLE CAUSE: token_v2 or file_token may have expired. "
            "Check logs - if this error persists, renew NOTION_TOKEN_V2 and NOTION_FILE_TOKEN in .env. "
            "See README for instructions on how to extract new cookies from browser."
        )
        notify(
            "Notion backup: Track 2 FAILED",
            "Could not start HTML export. token_v2 or file_token may have expired.\n"
            "Renew them in .env and restart notion-backup.",
        )
        return False

    task_id = resp.json().get("taskId")
    if not task_id:
        log.error("[Track2] No taskId returned: %s", resp.text[:200])
        return False

    log.info("[Track2] Task enqueued: %s — polling every %ds (timeout %dmin)...",
             task_id, POLL_INTERVAL_S, POLL_TIMEOUT_S // 60)

    # ── Poll for completion ─────────────────────────────────────────────────
    deadline     = time.monotonic() + POLL_TIMEOUT_S
    download_url = None

    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_S)
        try:
            poll = session.post(
                f"{EXPORT_BASE}/getTasks",
                json={"taskIds": [task_id]},
                timeout=30,
            )
            poll.raise_for_status()
        except requests.RequestException as exc:
            log.warning("[Track2] Poll request failed: %s — retrying...", exc)
            continue

        tasks = poll.json().get("results", [])
        if not tasks:
            continue

        task   = tasks[0]
        state  = task.get("state")
        log.info("[Track2] Task state: %s", state)

        if state == "success":
            download_url = task.get("status", {}).get("exportURL")
            if not download_url:
                # Notion API (post-2024) no longer returns exportURL in the response.
                # The download link is sent to the account email instead.
                log.info(
                    "[Track2] Export triggered successfully — Notion will email the "
                    "HTML download link to your account. Check your inbox."
                )
                return True
            break
        if state == "failure":
            log.error("[Track2] Export task failed: %s", task.get("error", "no details"))
            log.warning(
                "[Track2] POSSIBLE CAUSE: token_v2 or file_token may have expired. "
                "Renew NOTION_TOKEN_V2 and NOTION_FILE_TOKEN in .env and restart the container."
            )
            notify(
                "Notion backup: Track 2 FAILED",
                f"Export task failed: {task.get('error', 'no details')}.\n"
                "token_v2 or file_token may have expired — renew in .env.",
            )
            return False

    if not download_url:
        log.error("[Track2] Timed out after %d minutes waiting for export", POLL_TIMEOUT_S // 60)
        return False

    # ── Download ZIP ────────────────────────────────────────────────────────
    log.info("[Track2] Downloading export ZIP...")
    try:
        dl = session.get(download_url, stream=True, timeout=300)
        dl.raise_for_status()
    except requests.RequestException as exc:
        log.error("[Track2] Download failed: %s", exc)
        log.warning(
            "[Track2] POSSIBLE CAUSE: token_v2 or file_token may have expired. "
            "If this error persists, renew NOTION_TOKEN_V2 and NOTION_FILE_TOKEN in .env."
        )
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    MD_ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = MD_ARCHIVES_DIR / f"notion-export-{timestamp}.zip"

    with open(zip_path, "wb") as f:
        for chunk in dl.iter_content(chunk_size=65536):
            f.write(chunk)
    log.info("[Track2] ZIP saved: %s (%d bytes)", zip_path.name, zip_path.stat().st_size)

    # ── Extract into latest/ ────────────────────────────────────────────────
    if MD_LATEST_DIR.exists():
        shutil.rmtree(MD_LATEST_DIR)
    MD_LATEST_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(MD_LATEST_DIR)
    log.info("[Track2] Extracted to %s", MD_LATEST_DIR)

    # ── Rotate archives (keep last 3) ───────────────────────────────────────
    archives = sorted(MD_ARCHIVES_DIR.glob("notion-export-*.zip"))
    for old in archives[:-3]:
        old.unlink()
        log.info("[Track2] Removed old archive: %s", old.name)

    log.info("[Track2] Native export complete.")
    return True


# ════════════════════════════════════════════════════════════════════════════
# Hardlink snapshots (daily 7 days · weekly 8 weeks)
# ════════════════════════════════════════════════════════════════════════════

def _hardlink_tree(src: Path, dst: Path) -> None:
    """
    Copy a directory tree from src to dst using hardlinks for all files.
    Directories are created normally; files share inodes with the source.
    Combined with the unlink-before-write pattern in save_json, this means
    future writes to src do NOT affect any previously created snapshot.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        relative = item.relative_to(src)
        target   = dst / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(item, target)


def take_snapshots() -> None:
    """
    Create hardlink snapshots of the current JSON backup.

    Policy:
      - Daily  : one snapshot per calendar day, keep last KEEP_DAILY (7)
      - Weekly : one snapshot per ISO week,     keep last KEEP_WEEKLY (8)

    Snapshots that already exist for today / this week are skipped silently
    so re-running the script on the same day is idempotent.
    """
    if not JSON_DIR.exists():
        log.warning("[snapshots] JSON dir missing — nothing to snapshot")
        return

    SNAP_DAILY_DIR.mkdir(parents=True,  exist_ok=True)
    SNAP_WEEKLY_DIR.mkdir(parents=True, exist_ok=True)

    today    = datetime.now(timezone.utc).date()
    iso      = today.isocalendar()
    day_key  = today.isoformat()                      # 2025-01-17
    week_key = f"{iso.year}-W{iso.week:02d}"          # 2025-W03

    # ── Daily ─────────────────────────────────────────────────────────────
    day_snap = SNAP_DAILY_DIR / day_key
    if day_snap.exists():
        # Remove any pre-existing directory (could be a partial copy from a
        # crashed previous run) before recreating it cleanly.
        shutil.rmtree(day_snap)
        log.info("[snapshots] Removed incomplete/existing daily snapshot: %s", day_key)
    log.info("[snapshots] Creating daily snapshot: %s", day_key)
    _hardlink_tree(JSON_DIR, day_snap)
    log.info("[snapshots] Daily snapshot ready: %s", day_key)

    # Rotate: keep last KEEP_DAILY entries (sorted lexicographically = chronologically)
    daily_snaps = sorted(p for p in SNAP_DAILY_DIR.iterdir() if p.is_dir())
    for old in daily_snaps[:-KEEP_DAILY]:
        shutil.rmtree(old)
        log.info("[snapshots] Removed old daily snapshot: %s", old.name)

    # ── Weekly ────────────────────────────────────────────────────────────
    # Only create once per ISO week — unlike daily, do NOT overwrite if one
    # already exists (weekly snapshots are a stable historical record).
    week_snap = SNAP_WEEKLY_DIR / week_key
    if not week_snap.exists():
        log.info("[snapshots] Creating weekly snapshot: %s", week_key)
        _hardlink_tree(JSON_DIR, week_snap)
        log.info("[snapshots] Weekly snapshot ready: %s", week_key)
    else:
        log.info("[snapshots] Weekly snapshot already exists: %s", week_key)

    # Rotate: keep last KEEP_WEEKLY entries
    weekly_snaps = sorted(p for p in SNAP_WEEKLY_DIR.iterdir() if p.is_dir())
    for old in weekly_snaps[:-KEEP_WEEKLY]:
        shutil.rmtree(old)
        log.info("[snapshots] Removed old weekly snapshot: %s", old.name)


# ════════════════════════════════════════════════════════════════════════════
# Git versioning
# ════════════════════════════════════════════════════════════════════════════

def git_commit(page_count: int, db_count: int) -> None:
    """Commit all changes in /backup to git; optionally push to remote."""
    cwd = str(BACKUP_DIR)

    def run(*cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True)

    # Init repo if needed
    if not (BACKUP_DIR / ".git").exists():
        log.info("[git] Initializing repo in %s", cwd)
        run("git", "init")
        run("git", "config", "user.name",  "notion-backup")
        run("git", "config", "user.email", "backup@localhost")

    run("git", "add", ".")

    status = run("git", "status", "--porcelain")
    if not status.stdout.strip():
        log.info("[git] No changes — skipping commit.")
        return

    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = f"backup {ts} — {page_count} pages, {db_count} databases"
    result = run("git", "commit", "-m", msg)
    if result.returncode != 0:
        log.error("[git] Commit failed:\n%s", result.stderr)
        return
    log.info("[git] Committed: %s", msg)

    if GIT_REMOTE_URL:
        log.info("[git] Pushing to remote...")
        check = run("git", "remote", "get-url", "origin")
        if check.returncode == 0:
            run("git", "remote", "set-url", "origin", GIT_REMOTE_URL)
        else:
            run("git", "remote", "add", "origin", GIT_REMOTE_URL)
        push = run("git", "push", "origin", "HEAD:main", "--force")
        if push.returncode == 0:
            log.info("[git] Push complete.")
        else:
            log.error("[git] Push failed:\n%s", push.stderr)


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    log.info("=== notion-backup starting ===")

    api_session = requests.Session()
    api_session.headers.update({
        "Authorization":  f"Bearer {NOTION_API_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type":   "application/json",
    })

    # Shared result state (written by workers, read by main thread after join)
    results: dict = {
        "page_count": 0,
        "db_count":   0,
        "t1_ok":      False,
        "t2_ok":      False,
    }

    def track1_worker() -> None:
        try:
            pages, dbs = run_track1(api_session)
            results["page_count"] = pages
            results["db_count"]   = dbs
            results["t1_ok"]      = True
        except Exception as exc:
            log.error("[Track1] Fatal: %s", exc)

    def track2_worker() -> None:
        try:
            results["t2_ok"] = run_track2()
        except Exception as exc:
            log.error("[Track2] Fatal: %s", exc)

    t1 = threading.Thread(target=track1_worker, name="Track1", daemon=True)
    t2 = threading.Thread(target=track2_worker, name="Track2", daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    log.info(
        "Tracks complete — JSON backup: %s | Native export: %s",
        "OK" if results["t1_ok"] else "FAILED",
        "OK" if results["t2_ok"] else "skipped/failed",
    )

    if not results["t1_ok"]:
        notify(
            "Notion backup: Track 1 FAILED",
            "JSON backup via official API did not complete. Check container logs.",
        )

    if not results["t2_ok"] and all([NOTION_TOKEN_V2, NOTION_SPACE_ID]):
        log.warning(
            "=== Track 2 FAILED === "
            "If this is the first failure, it likely means token_v2 and/or file_token have expired. "
            "Check the logs above for details. "
            "To renew: open notion.so in a browser, open DevTools → Application → Cookies, "
            "copy new token_v2 and file_token values, update .env, then restart the container."
        )

    # Snapshots and git commit only run when Track 1 produced valid data
    if results["t1_ok"]:
        try:
            take_snapshots()
        except Exception as exc:
            log.error("[snapshots] Error during snapshot rotation: %s", exc)
        git_commit(results["page_count"], results["db_count"])
    else:
        log.warning("[git] Skipping git commit — Track 1 did not complete successfully")
    log.info("=== notion-backup done ===")


if __name__ == "__main__":
    main()
