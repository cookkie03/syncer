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
