#!/usr/bin/env python3
"""Backup, inspect, verify, or export a single Todoist account."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from api import Client
from collector import Collector, parse_date
from storage import Store
from layout import migrate, publish, verify_snapshot


@contextmanager
def exclusive_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "backup.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another backup is already running") from None
        yield


def healthy(store, *, now=None):
    now = now or datetime.now(timezone.utc)
    progress = store.get("last_progress")
    if not progress or now - parse_date(progress) > timedelta(hours=12):
        return False
    run = store.db.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    # A fatal authentication/storage error is unhealthy immediately. Coverage limitations are reported separately.
    return bool(run and run[0] != "failed" and not store.verify(quick=True))


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["backup", "status", "verify", "export"], nargs="?", default="backup")
    parser.add_argument("--state-dir", "--data-dir", dest="state_dir", default=os.getenv("TODOIST_STATE_DIR", os.getenv("TODOIST_DATA_DIR", str(Path(__file__).parent / "state"))))
    parser.add_argument("--backup-dir", default=os.getenv("TODOIST_BACKUP_DIR", str(Path(__file__).parent / "backup")))
    parser.add_argument("--legacy-dir", default=os.getenv("TODOIST_LEGACY_DIR", str(Path(__file__).parent / "data")))
    parser.add_argument("--output", help="Export parent directory (default: backup/exports)")
    parser.add_argument("--health", action="store_true", help="Status exit code reflects progress and integrity")
    args = parser.parse_args(argv)
    store = None
    try:
        if args.command == "backup":
            token = os.getenv("TODOIST_API_TOKEN", "").strip()
            if not token or token == "YOUR_VALUE_HERE" or token.startswith("${"):
                print("ERROR: TODOIST_API_TOKEN is required; configure .env locally", file=sys.stderr)
                return 1
            with exclusive_lock(args.state_dir):
                migrate(args.legacy_dir, args.state_dir, args.backup_dir)
                store = Store(args.state_dir, object_root=Path(args.backup_dir) / "objects")
                client = Client(token, max_requests=int(os.getenv("TODOIST_MAX_REQUESTS", "600")),
                                max_seconds=int(os.getenv("TODOIST_MAX_RUN_SECONDS", "1800")))
                collector = Collector(store, client, max_history_windows=int(os.getenv("TODOIST_HISTORY_WINDOWS", "20")))
                status = collector.run()
                if status != "failed":
                    try:
                        publish(store, args.backup_dir)
                    except (OSError, sqlite3.Error, ValueError, RuntimeError):
                        with store.db:
                            store.db.execute("UPDATE runs SET status='failed',error='Snapshot publication failed' WHERE id=?", (collector.run_id,))
                        raise
                print(f"Todoist backup: {status}")
                print(json.dumps(store.status(), ensure_ascii=False, indent=2))
                return 1 if status == "failed" else 0
        store = Store(args.state_dir, readonly=True, object_root=Path(args.backup_dir) / "objects")
        if args.command == "status":
            print(json.dumps(store.status(), ensure_ascii=False, indent=2))
            return 0 if not args.health or (healthy(store) and (Path(args.backup_dir) / "current").is_dir()) else 1
        if args.command == "verify":
            errors = store.verify() + verify_snapshot(args.backup_dir)
            print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
            return 1 if errors else 0
        print(store.export(args.output or Path(args.backup_dir) / "exports"))
        return 0
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        # Do not include exception text: it may contain private URLs or response fields.
        print("ERROR: operation failed; check configuration, status, and local storage", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
