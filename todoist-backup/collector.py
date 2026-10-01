"""Resumable account collection. Checkpoints always follow committed data."""

from __future__ import annotations

import calendar
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from api import APIError, AuthenticationError, BudgetReached
from storage import file_hash, utcnow


def parse_date(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)


def completion_window_end(start, target):
    # Todoist specifies three calendar months; February can make that shorter than 90 days.
    year, month = divmod(start.year * 12 + start.month - 1 + 3, 12)
    month += 1
    calendar_end = start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))
    return min(start + timedelta(days=90), calendar_end, target)


def page_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for name in ("results", "items", "workspace_users", "sections", "projects", "events", "comments", "collaborators"):
            if name in data and isinstance(data[name], list):
                return data[name]
    raise APIError(200, "unknown_page_shape")


class Collector:
    def __init__(self, store, client, *, now=None, max_history_windows=20):
        self.store, self.client = store, client
        self.now = now or datetime.now(timezone.utc)
        self.run_id = None
        if max_history_windows <= 0:
            raise ValueError("History window budget must be positive")
        self.max_history_windows = max_history_windows

    def optional(self, source, action):
        try:
            action()
        except AuthenticationError:
            raise
        except APIError as exc:
            with self.store.db:
                status = "unavailable" if exc.status in (403, 404) else "failed"
                if source == "native_backups" and exc.status in (400, 401):
                    status = "unavailable"
                if source == "archived_sections" and exc.status == 400:
                    status = "unavailable"
                detail = str(exc)
                if source == "native_backups" and exc.status in (400, 401, 403):
                    detail += "; automatic backups may require a paid plan or an MFA token"
                self.store.cover(source, status, detail)
            return False
        with self.store.db:
            self.store.cover(source, "complete")
        return True

    def pages(self, source, path, collection, params=None):
        for data in self.client.pages(path, params):
            records = page_items(data)
            with self.store.db:
                self.store.response(source, data, self.run_id)
                for value in records:
                    identifier = None
                    if not isinstance(value, dict):
                        identifier = str(value)
                        value = {"value": value}
                    if collection == "completed_tasks":
                        identifier = f"{value['id']}:{value.get('completed_at') or value.get('completed_date') or 'unknown'}"
                    elif collection == "workspace_users":
                        identifier = f"{value.get('workspace_id')}:{value.get('user_id')}"
                    self.store.put(collection, value, self.run_id, record_id=identifier)
                self.store.progress()

    def sync(self):
        # Check identity on every run before reading or changing the local account state.
        user = self.client.read("user")
        if not isinstance(user, dict) or user.get("id") is None:
            raise ValueError("User endpoint returned no account identity")
        identity = self.store.get("account_id")
        if identity is not None and str(user["id"]) != identity:
            raise ValueError("Account identity mismatch; existing backup preserved")
        with self.store.db:
            self.store.set("account_id", str(user["id"]))
            self.store.put("userinfo", user, self.run_id, record_id="singleton")
        token = self.store.get("sync_token", "*")
        try:
            data = self.client.read("sync", sync_token=token)
        except AuthenticationError:
            raise
        except APIError as exc:
            if token == "*" or exc.status != 400:
                raise
            # A rejected old token can be safely rebaselined after the identity check.
            data = self.client.read("sync", sync_token="*")
        self.store.ingest_sync(data, self.run_id)
        if data.get("full_sync"):
            # Follow a full sync immediately because large-account snapshots can lag.
            self.store.ingest_sync(self.client.read("sync", sync_token=self.store.get("sync_token")), self.run_id)

    def history(self):
        if self.store.get("history_target") is None:
            users = self.store.records("user")
            user = users[0][1] if users else {}
            joined = user.get("joined_at") or user.get("created_at") or user.get("added_at")
            if not joined:
                profiles = self.store.records("userinfo")
                joined = profiles[0][1].get("joined_at") if profiles else None
            with self.store.db:
                if not joined:
                    # Todoist launched in 2007. A conservative scan avoids guessing a recent start.
                    joined = "2007-01-01T00:00:00Z"
                    self.store.cover("account_creation_date", "unavailable", "Not returned; scan history from 2007-01-01")
                self.store.set("history_next", parse_date(joined).isoformat())
                self.store.set("history_origin", parse_date(joined).isoformat())
                self.store.set("history_target", self.now.isoformat())
        start = parse_date(self.store.get("history_next"))
        target = parse_date(self.store.get("history_target"))
        for _ in range(self.max_history_windows):
            if start >= target:
                break
            end = completion_window_end(start, target)
            source = "completed_history"
            if not self.optional(source, lambda: self.pages(source, "tasks/completed/by_completion_date", "completed_tasks",
                                {"since": start.isoformat(), "until": end.isoformat()})):
                return
            # Do not save pagination cursors: replay a failed window, deduplicating its records.
            with self.store.db:
                self.store.set("history_next", end.isoformat())
                self.store.progress()
            start = end
        with self.store.db:
            complete = start >= target
            self.store.set("history_complete", complete)
            self.store.cover("completed_history", "complete" if complete else "backfill_in_progress",
                             f"Scanned through {start.isoformat()}; target {target.isoformat()}")

    def recent(self):
        # Capture the interval since the last successful recent scan, plus 48h overlap.
        last = self.store.get("recent_until", self.now.isoformat())
        start = parse_date(last) - timedelta(days=2)
        while start < self.now:
            end = completion_window_end(start, self.now)
            if not self.optional("recent_completions", lambda: self.pages("recent_completions",
                                "tasks/completed/by_completion_date", "completed_tasks",
                                {"since": start.isoformat(), "until": end.isoformat()})):
                return
            with self.store.db:
                self.store.set("recent_until", end.isoformat())
            start = end

    def activity(self):
        previous = self.store.get("activity_until")
        start = parse_date(previous) - timedelta(days=2) if previous else self.now - timedelta(days=2)
        while start < self.now:
            end = min(start + timedelta(days=90), self.now)
            self.activity_pages("activity", start, end)
            with self.store.db:
                self.store.set("activity_until", end.isoformat())
            start = end

    def activity_pages(self, source, start, end):
        # Date filters avoid depending on undocumented response ordering.
        for data in self.client.pages("activities", {"date_from": start.isoformat(), "date_to": end.isoformat()}):
            values = page_items(data)
            with self.store.db:
                self.store.response(source, data, self.run_id)
                for value in values:
                    self.store.put("activities", value, self.run_id)
                self.store.progress()

    def activity_history(self):
        start = parse_date(self.store.get("activity_history_next", self.store.get("history_origin", self.store.get("history_next"))))
        target = parse_date(self.store.get("history_target"))
        for _ in range(self.max_history_windows):
            if start >= target:
                break
            end = min(start + timedelta(days=90), target)
            if not self.optional("activity_history_window", lambda: self.activity_pages("activity_history", start, end)):
                return
            with self.store.db:
                self.store.set("activity_history_next", end.isoformat())
                self.store.progress()
            start = end
        with self.store.db:
            self.store.cover("activity_history", "complete" if start >= target else "backfill_in_progress",
                             f"Scanned through {start.isoformat()}; target {target.isoformat()}")

    def full_project(self, identifier):
        source = f"project_details:{identifier}"
        path = f"projects/{quote(identifier, safe='')}"
        try:
            # Mentioned in Todoist's migration guide, but absent from current OpenAPI.
            data = self.client.read(path + "/full")
        except AuthenticationError:
            raise
        except APIError as exc:
            if exc.status not in (403, 404):
                raise
            with self.store.db:
                project_rows = [v for i, v, _, _ in self.store.records("projects") if i == identifier]
                archived = bool(project_rows and project_rows[0].get("is_archived"))
                self.store.cover(f"project_archive_contents:{identifier}", "unavailable" if archived else "not_applicable",
                                 "Legacy full-project endpoint unavailable; use documented project/task/section reads")
            project = self.client.read(path)
            with self.store.db:
                self.store.put("projects", project, self.run_id)
                self.store.response(source, project, self.run_id)
                self.store.progress()
            self.pages(source, "tasks", "items", {"project_id": identifier})
            self.pages(source, "sections", "sections", {"project_id": identifier})
            return
        if not isinstance(data, dict):
            raise APIError(200, "unknown_project_shape")
        aliases = {"items": "items", "tasks": "items", "sections": "sections", "notes": "notes",
                   "project_notes": "project_notes", "comments": "project_notes", "project": "projects"}
        with self.store.db:
            self.store.response(source, data, self.run_id)
            # Preserve all payloads even when Todoist adds a field the collector doesn't know yet.
            for key, collection in aliases.items():
                values = data.get(key, [])
                if isinstance(values, dict):
                    values = [values]
                for value in values:
                    if isinstance(value, dict):
                        self.store.put(collection, value, self.run_id)
            self.store.progress()

    def one_task(self, identifier):
        task = self.client.read(f"tasks/{quote(identifier, safe='')}")
        with self.store.db:
            self.store.put("historical_task_details", task, self.run_id)
            self.store.response(f"task_details:{identifier}", task, self.run_id)
            self.store.progress()

    def daily(self):
        last = self.store.get("daily_until")
        jobs = self.store.get("daily_jobs")
        if jobs is None and last and self.now - parse_date(last) < timedelta(days=1):
            return
        if jobs is None:
            # Inventory must finish before declaring a reconciliation cycle complete.
            inventories = [
                ("archived_projects", "projects/archived", "projects"),
                ("archived_sections", "sections/archived", "sections"),
                ("workspace_users", "workspaces/users", "workspace_users"),
                ("shared_labels", "labels/shared", "shared_labels"),
                ("workspaces", "workspaces", "workspaces"),
            ]
            for source, path, collection in inventories:
                self.optional(source, lambda s=source, p=path, c=collection: self.pages(s, p, c))
            self.optional("productivity_stats", self.stats)
            self.optional("project_permissions", lambda: self.singleton("project_permissions", "projects/permissions"))
            for identifier, workspace, deleted, _ in self.store.records("workspaces"):
                if deleted:
                    continue
                self.optional(f"folders:{identifier}", lambda w=identifier: self.pages(f"folders:{w}", "folders", "folders", {"workspace_id": w}))
                self.optional(f"workspace_invitations:{identifier}", lambda w=identifier: self.pages(f"workspace_invitations:{w}", "workspaces/invitations/all", "workspace_invitations", {"workspace_id": w}))
                self.optional(f"workspace_plan:{identifier}", lambda w=identifier: self.singleton(f"workspace_plan:{w}", "workspaces/plan_details", {"workspace_id": w}))
            projects = [(identifier, value) for identifier, value, deleted, availability in self.store.records("projects") if not deleted]
            jobs = []
            for identifier, value in projects:
                jobs.extend([["project_details", identifier], ["project_comments", identifier]])
                if value.get("is_shared"):
                    jobs.append(["collaborators", identifier])
            jobs.append(["task_inventory", ""])
            with self.store.db:
                self.store.set("daily_jobs", jobs)
        while jobs:
            kind, identifier = jobs[0]
            source = f"{kind}:{identifier}"
            if kind == "task_inventory":
                # Build this after project collection so new archived tasks receive comments too.
                active = {i for i, value, deleted, _ in self.store.records("items")
                          if not deleted and not value.get("checked") and not value.get("is_archived")}
                archived_projects = {i for i, value, _, _ in self.store.records("projects") if value.get("is_archived")}
                active.difference_update(i for i, value, _, _ in self.store.records("items") if value.get("project_id") in archived_projects)
                all_ids = {i for i, _, deleted, _ in self.store.records("items") if not deleted}
                all_ids.update(str(value["id"]) for _, value, _, _ in self.store.records("completed_tasks"))
                jobs = jobs[1:] + [["task_details", i] for i in sorted(active)] + [["task_comments", i] for i in sorted(all_ids)]
                with self.store.db:
                    self.store.set("daily_jobs", jobs)
                continue
            if kind == "project_details":
                action = lambda: self.full_project(identifier)
            elif kind == "task_details":
                action = lambda: self.one_task(identifier)
            elif kind == "collaborators":
                action = lambda: self.pages(source, f"projects/{quote(identifier, safe='')}/collaborators", "collaborators")
            else:
                field, collection = ("project_id", "project_notes") if kind == "project_comments" else ("task_id", "notes")
                action = lambda: self.pages(source, "comments", collection, {field: identifier})
            ok = self.optional(source, action)
            if not ok:
                coverage = self.store.db.execute("SELECT status FROM coverage WHERE source=?", (source,)).fetchone()[0]
                if coverage == "failed":
                    # Retry temporary failures next run, without restarting already completed jobs.
                    return
                with self.store.db:
                    collection = "projects" if kind.startswith("project") else "historical_task_details"
                    self.store.missing(collection, identifier)
            jobs = jobs[1:]
            with self.store.db:
                self.store.set("daily_jobs", jobs)
        with self.store.db:
            self.store.set("daily_jobs", None)
            self.store.set("daily_until", self.now.isoformat())
            self.store.cover("daily_reconciliation", "complete")

    def stats(self):
        self.singleton("productivity_stats", "tasks/completed/stats")

    def singleton(self, source, path, params=None):
        data = self.client.read(path, params)
        with self.store.db:
            self.store.response(source, data, self.run_id)
            self.store.put(source, data, self.run_id, record_id="singleton")
            self.store.progress()

    def native_backups(self):
        data = self.client.read("backups")
        values = page_items(data)
        with self.store.db:
            self.store.response("native_backups", data, self.run_id)
            for value in values:
                self.store.put("native_backups", value, self.run_id, record_id=value.get("version") or value.get("url"))
                if value.get("url"):
                    self.store.queue_file("native_backups", value["url"], value.get("version", "backup.zip"), archive=True)
            self.store.progress()

    def files(self):
        last_verified = self.store.get("last_files_verified")
        verify_hashes = not last_verified or self.now - parse_date(last_verified) >= timedelta(days=1)
        rows = list(self.store.db.execute("SELECT * FROM files ORDER BY CASE status WHEN 'complete' THEN 1 ELSE 0 END,ref"))
        for row in rows:
            if row["status"] == "complete":
                path = self.store.object_root / row["hash"][:2] / row["hash"]
                if path.is_file() and path.stat().st_size == row["size"] and (not verify_hashes or file_hash(path) == row["hash"]):
                    continue
            self.store.object_root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.store.object_root, suffix=".download", delete=False) as output:
                temp_path = Path(output.name)
            try:
                self.client.download(row["url"], temp_path, archive=bool(row["archive"]))
                self.store.install_file(row, temp_path)
            except AuthenticationError:
                raise
            except (APIError, OSError) as exc:
                # No URLs, signed query strings, or raw exception messages in diagnostics.
                detail = str(exc) if isinstance(exc, APIError) else "Local file write failed"
                with self.store.db:
                    self.store.db.execute("UPDATE files SET status='failed',error=? WHERE ref=?", (detail, row["ref"]))
            finally:
                temp_path.unlink(missing_ok=True)
        count = self.store.db.execute("SELECT count(*) FROM files WHERE status!='complete'").fetchone()[0]
        with self.store.db:
            self.store.cover("files", "partial" if count else "complete", f"{count} pending or failed downloads")
            if verify_hashes and not count:
                self.store.set("last_files_verified", self.now.isoformat())

    def run(self):
        with self.store.db:
            self.store.db.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'", (utcnow(),))
            self.run_id = self.store.db.execute("INSERT INTO runs(started_at,status) VALUES(?,'running')", (utcnow(),)).lastrowid
        error = None
        try:
            self.sync()
            self.recent()
            self.optional("activity", self.activity)
            self.history()
            self.activity_history()
            self.daily()
            self.optional("native_backups", self.native_backups)
            self.files()
            with self.store.db:
                pending = self.store.get("daily_jobs") is not None
                self.store.cover("daily_reconciliation", "backfill_in_progress" if pending else "complete")
            integrity = self.store.verify(quick=True)
            if integrity:
                raise ValueError("Local backup integrity check failed; run verify")
            coverage = self.store.status()["coverage"]
            if any(row["status"] in {"failed", "unavailable", "partial"} for row in coverage):
                status = "partial"
            elif not self.store.get("history_complete") or self.store.get("daily_jobs") is not None or any(row["status"] == "backfill_in_progress" for row in coverage if row["source"] != "run_budget"):
                status = "backfill_in_progress"
            else:
                status = "complete"
        except BudgetReached:
            status = "backfill_in_progress"
            with self.store.db:
                self.store.cover("run_budget", status, "Run budget reached; checkpoints retained for next run")
        except (APIError, ValueError, OSError) as exc:
            status = "failed"
            error = str(exc) if isinstance(exc, (APIError, ValueError)) else "Local storage failure"
        except Exception:
            # Record unexpected failures without exposing credentials or private response text.
            with self.store.db:
                self.store.db.execute("UPDATE runs SET status='failed',finished_at=?,error='Unexpected collector failure' WHERE id=?",
                                      (utcnow(), self.run_id))
            raise
        with self.store.db:
            if status != "backfill_in_progress":
                self.store.cover("run_budget", "complete")
            self.store.db.execute("UPDATE runs SET status=?,finished_at=?,error=? WHERE id=?",
                                  (status, utcnow(), error, self.run_id))
            if status in {"complete", "partial"}:
                self.store.set("last_finished_collection", utcnow())
        return status
