"""Offline behavior tests: recovery, account boundaries, incremental history, and files."""

import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
from api import APIError, AuthenticationError, BudgetReached, Client, public_url
from backup import exclusive_lock, healthy, main
from collector import Collector, completion_window_end, page_items
from storage import Store, file_hash


NOW = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.account = "user-1"
        self.sync_data = {"user": {"id": "user-1", "joined_at": "2026-01-01T00:00:00Z"},
                          "sync_token": "next-token", "items": [{"id": "task-1", "content": "café 🚆"}],
                          "projects": [{"id": "project-1", "name": "Demo"}]}
        self.routes = {}
        self.page_routes = {}
        self.file_error = None

    def read(self, path, params=None, *, sync_token=None):
        self.calls.append((path, params, sync_token))
        if path in self.routes:
            value = self.routes[path]
            if isinstance(value, Exception):
                raise value
            return value
        if path == "user":
            return {"id": self.account}
        if path == "sync":
            return self.sync_data
        if path == "backups":
            return []
        if path == "tasks/completed/stats":
            return {"karma": 100}
        if path == "projects/permissions":
            return {"admin": ["read"]}
        if path.endswith("/full"):
            return {"project": {"id": "project-1", "name": "Demo"}, "items": []}
        if path.startswith("tasks/"):
            return {"id": path.split("/")[-1], "content": "café 🚆"}
        raise AssertionError(path)

    def pages(self, path, params=None):
        self.calls.append((path, params, None))
        for value in self.page_routes.get(path, [{"results": [], "next_cursor": None}]):
            if isinstance(value, Exception):
                raise value
            yield value

    def download(self, url, path, *, archive=False):
        if self.file_error:
            raise self.file_error
        Path(path).write_bytes(b"demo file bytes")
        return 15


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.client = FakeClient()
        self.collector = Collector(self.store, self.client, now=NOW)
        self.collector.run_id = 1

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def ingest(self, **values):
        data = dict(self.client.sync_data, **values)
        self.store.ingest_sync(data, 1)

    def test_updates_deletions_and_return_to_old_content_preserve_revisions(self):
        self.ingest()
        self.ingest()
        self.ingest(items=[{"id": "task-1", "content": "changed"}])
        self.ingest()
        self.ingest(items=[{"id": "task-1", "is_deleted": True}])
        rows = list(self.store.db.execute("SELECT * FROM revisions WHERE collection='items'"))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["hash"], rows[2]["hash"])
        self.assertTrue(self.store.records("items")[0][2])

    def test_token_rolls_back_when_resource_ingestion_fails(self):
        self.ingest()
        with patch.object(self.store, "put", side_effect=ValueError("interrupted")):
            with self.assertRaises(ValueError):
                self.ingest(sync_token="must-not-commit", items=[{"id": "new"}])
        self.assertEqual(self.store.get("sync_token"), "next-token")
        self.assertEqual([r[0] for r in self.store.records("items")], ["task-1"])

    def test_account_mismatch_preserves_all_data_and_token(self):
        self.ingest()
        with self.assertRaises(ValueError):
            self.ingest(user={"id": "other"}, sync_token="wrong")
        self.assertEqual(self.store.get("account_id"), "user-1")
        self.assertEqual(self.store.get("sync_token"), "next-token")

    def test_empty_incremental_resources_do_not_erase_records(self):
        self.ingest()
        self.ingest(items=[], user=None)
        self.assertEqual(len(self.store.records("items")), 1)
        self.assertEqual(self.store.records("user")[0][1]["id"], "user-1")

    def test_full_resync_preserves_missing_records_without_inventing_deletions(self):
        self.ingest()
        self.ingest(full_sync=True, items=[])
        record = self.store.records("items")[0]
        self.assertFalse(record[2])
        self.assertEqual(record[3], "absent_from_active_sync")

    def test_api_credentials_excluded_from_payloads(self):
        self.ingest(user={"id": "user-1", "token": "secret", "api_token": "secret"})
        payloads = " ".join(r[0] for r in self.store.db.execute("SELECT json FROM payloads"))
        self.assertNotIn('"secret"', payloads)
        self.assertNotIn("next-token", payloads)

    def test_full_sync_followed_by_incremental_and_saved_token_reused(self):
        self.client.sync_data["full_sync"] = True
        self.collector.sync()
        tokens = [call[2] for call in self.client.calls if call[0] == "sync"]
        self.assertEqual(tokens, ["*", "next-token"])
        self.collector.sync()
        self.assertEqual([call[2] for call in self.client.calls if call[0] == "sync"][-2:], ["next-token", "next-token"])

    def test_identity_checked_before_sync(self):
        self.ingest()
        self.client.account = "different"
        with self.assertRaises(ValueError):
            self.collector.sync()
        self.assertEqual([c[0] for c in self.client.calls], ["user"])

    def test_rejected_saved_sync_token_is_safely_rebaselined(self):
        self.ingest()
        original = self.client.read
        def read(path, params=None, *, sync_token=None):
            if path == "sync" and sync_token == "next-token":
                self.client.calls.append((path, params, sync_token))
                raise APIError(400)
            return original(path, params, sync_token=sync_token)
        self.client.read = read
        self.collector.sync()
        self.assertEqual([c[2] for c in self.client.calls if c[0] == "sync"], ["next-token", "*"])
        self.assertEqual(len(self.store.records("items")), 1)

    def test_access_loss_keeps_old_project(self):
        self.ingest()
        self.client.routes["projects/project-1/full"] = APIError(404)
        self.client.routes["projects/project-1"] = APIError(404)
        self.collector.daily()
        project = self.store.records("projects")[0]
        self.assertEqual(project[1]["name"], "Demo")
        self.assertFalse(project[2])
        self.assertEqual(project[3], "inaccessible")

    def test_history_windows_resume_after_failed_page_without_saved_cursor(self):
        self.ingest()
        self.client.page_routes["tasks/completed/by_completion_date"] = [
            {"items": [{"id": "done", "completed_at": "2026-01-05T00:00:00Z"}], "next_cursor": "page2"},
            APIError(503),
        ]
        self.collector.history()
        self.assertEqual(self.store.get("history_next"), "2026-01-01T00:00:00+00:00")
        self.assertEqual(len(self.store.records("completed_tasks")), 1)
        self.client.page_routes["tasks/completed/by_completion_date"] = [
            {"items": [{"id": "done", "completed_at": "2026-01-05T00:00:00Z"}], "next_cursor": None}]
        self.collector.history()
        self.assertTrue(self.store.get("history_complete"))
        self.assertEqual(len(self.store.records("completed_tasks")), 1)
        for path, params, _ in self.client.calls:
            if path == "tasks/completed/by_completion_date":
                since = datetime.fromisoformat(params["since"])
                until = datetime.fromisoformat(params["until"])
                self.assertLessEqual(until - since, timedelta(days=90))
                self.assertNotIn("cursor", params)

    def test_recurring_completions_have_distinct_event_ids(self):
        self.client.page_routes["tasks/completed/by_completion_date"] = [{"items": [
            {"id": "recurring", "completed_at": "2026-09-29T00:00:00Z"},
            {"id": "recurring", "completed_at": "2026-09-30T00:00:00Z"}], "next_cursor": None}]
        self.collector.recent()
        self.assertEqual(len(self.store.records("completed_tasks")), 2)

    def test_recent_scan_catches_up_after_long_outage(self):
        with self.store.db:
            self.store.set("recent_until", "2026-01-01T00:00:00+00:00")
        self.collector.recent()
        calls = [c for c in self.client.calls if c[0] == "tasks/completed/by_completion_date"]
        self.assertGreater(len(calls), 1)
        self.assertEqual(self.store.get("recent_until"), NOW.isoformat())

    def test_history_window_budget_marks_backfill_in_progress(self):
        self.ingest()
        self.collector.max_history_windows = 1
        self.collector.history()
        self.assertFalse(self.store.get("history_complete"))
        self.assertNotEqual(self.store.get("history_next"), self.store.get("history_target"))

    def test_short_calendar_months_do_not_exceed_api_date_range(self):
        start = datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.assertEqual(completion_window_end(start, NOW), datetime(2026, 5, 1, tzinfo=timezone.utc))
        end_of_month = datetime(2026, 1, 31, tzinfo=timezone.utc)
        self.assertEqual(completion_window_end(end_of_month, NOW), datetime(2026, 4, 30, tzinfo=timezone.utc))

    def test_activity_history_replays_failed_window_and_keeps_saved_events(self):
        self.ingest()
        self.collector.history()
        self.client.page_routes["activities"] = [
            {"results": [{"id": "event-1", "event_type": "completed", "object_id": "recurring"}]}, APIError(503)]
        self.collector.activity_history()
        self.assertIsNone(self.store.get("activity_history_next"))
        self.assertEqual(len(self.store.records("activities")), 1)
        self.client.page_routes["activities"] = [{"results": [{"id": "event-1", "event_type": "completed", "object_id": "recurring"}]}]
        self.collector.activity_history()
        self.assertEqual(self.store.get("activity_history_next"), self.store.get("history_target"))
        self.assertEqual(len(self.store.records("activities")), 1)

    def test_archived_resources_and_comments_are_collected(self):
        self.ingest()
        self.client.page_routes["projects/archived"] = [{"results": [{"id": "archive", "is_archived": True}]}]
        self.client.page_routes["sections/archived"] = [{"results": [{"id": "section", "is_archived": True}]}]
        self.client.routes["projects/archive/full"] = {"items": [{"id": "archived-task", "content": "keep"}],
                                                     "project_notes": [{"id": "project-comment", "content": "keep"}]}
        self.client.page_routes["comments"] = [{"results": [{"id": "comment", "content": "demo"}]}]
        self.collector.daily()
        self.assertIn("archive", [r[0] for r in self.store.records("projects")])
        self.assertIn("archived-task", [r[0] for r in self.store.records("items")])
        self.assertIn("section", [r[0] for r in self.store.records("sections")])
        self.assertTrue(self.store.records("project_notes"))

    def test_daily_jobs_resume_on_temporary_failure(self):
        self.ingest()
        self.client.routes["projects/project-1/full"] = APIError(503)
        self.collector.daily()
        self.assertEqual(self.store.get("daily_jobs")[0], ["project_details", "project-1"])
        self.client.routes.pop("projects/project-1/full")
        self.collector.daily()
        self.assertIsNone(self.store.get("daily_jobs"))

    def test_legacy_project_endpoint_falls_back_to_documented_reads(self):
        self.ingest()
        self.client.routes["projects/project-1/full"] = APIError(404)
        self.client.routes["projects/project-1"] = {"id": "project-1", "name": "Demo"}
        self.client.page_routes["tasks"] = [{"results": [{"id": "fallback-task", "content": "keep"}]}]
        self.collector.daily()
        self.assertIn("fallback-task", [r[0] for r in self.store.records("items")])
        comments = [params for path, params, _ in self.client.calls if path == "comments"]
        self.assertIn({"task_id": "fallback-task"}, comments)

    def test_shared_labels_return_strings(self):
        self.client.page_routes["labels/shared"] = [{"results": ["shared-label", "café"]}]
        self.collector.pages("shared_labels", "labels/shared", "shared_labels")
        self.assertEqual({r[0] for r in self.store.records("shared_labels")}, {"shared-label", "café"})

    def test_activity_date_overlap_does_not_depend_on_result_order(self):
        with self.store.db:
            self.store.set("activity_until", "2026-09-30T00:00:00+00:00")
        self.collector.activity()
        call = self.client.calls[-1]
        self.assertEqual(call[1]["date_from"], "2026-09-28T00:00:00+00:00")
        self.assertEqual(call[1]["date_to"], NOW.isoformat())

    def test_daily_file_verification_repairs_same_size_corruption(self):
        with self.store.db:
            self.store.queue_file("comment", "https://example.org/a", "file")
        self.collector.files()
        path = next((self.root / "objects").glob("*/*"))
        path.write_bytes(b"x" * path.stat().st_size)
        self.collector.now = NOW + timedelta(days=1)
        self.collector.files()
        self.assertEqual(self.store.verify(), [])

    def test_attachment_failure_recovery_and_content_deduplication(self):
        with self.store.db:
            self.store.put("notes", {"id": "comment", "file_attachment": {
                "file_url": "https://example.org/a", "file_name": "../../unsafe"}}, 1)
            self.store.queue_file("another-comment", "https://example.org/b", "other")
        self.client.file_error = APIError(503)
        self.collector.files()
        self.assertEqual(self.store.status()["files"], {"failed": 2})
        self.client.file_error = None
        self.collector.files()
        self.assertEqual(self.store.status()["files"], {"complete": 2})
        self.assertEqual(len(list((self.root / "objects").glob("*/*"))), 1)
        self.assertEqual(self.store.verify(), [])

    def test_missing_and_corrupt_objects_are_detected_and_missing_file_retried(self):
        with self.store.db:
            self.store.queue_file("comment", "https://example.org/a", "file")
        self.collector.files()
        path = next((self.root / "objects").glob("*/*"))
        path.write_bytes(b"x" * path.stat().st_size)
        self.assertTrue(self.store.verify())
        path.unlink()
        self.assertTrue(self.store.verify(quick=True))
        self.collector.files()
        self.assertEqual(self.store.verify(), [])

    def test_corrupt_payload_is_detected(self):
        self.ingest()
        with self.store.db:
            self.store.db.execute("UPDATE payloads SET json='{}' WHERE hash=(SELECT hash FROM records WHERE collection='items')")
        self.assertTrue(self.store.verify())

    def test_export_contains_revisions_coverage_original_payloads_and_safe_csv(self):
        self.ingest(items=[{"id": "task", "content": "=SUM(1,1)", "description": "café 🚆", "labels": ["demo"]}])
        output = self.store.export(self.root / "exports")
        data = json.loads((output / "account.json").read_text())
        self.assertEqual(data["records"]["items"][0]["payload"]["description"], "café 🚆")
        self.assertTrue(data["revisions"])
        self.assertTrue(data["responses"])
        self.assertTrue((output / "COMPLETE").exists())
        with (output / "tasks.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["content"], "'=SUM(1,1)")
        self.assertNotEqual(output, self.store.export(self.root / "exports"))

    def test_export_file_references_work_after_directory_transfer(self):
        with self.store.db:
            self.store.queue_file("comment", "https://example.org/a", "file")
        self.collector.files()
        output = self.store.export(self.root / "exports")
        data = json.loads((output / "account.json").read_text())
        reference = data["files"][0]
        self.assertTrue((output / reference["export_relative_path"]).is_file())
        self.assertTrue((self.root / reference["object_path"]).is_file())

    def test_two_end_to_end_runs_are_complete_and_incremental(self):
        self.assertEqual(self.collector.run(), "complete")
        previous = self.store.status()["revisions"]
        self.client.sync_data = {"sync_token": "third-token", "items": [{"id": "task-1", "content": "updated"}]}
        self.assertEqual(self.collector.run(), "complete")
        self.assertEqual(self.store.get("sync_token"), "third-token")
        self.assertGreater(self.store.status()["revisions"], previous)
        self.assertEqual(self.store.verify(), [])

    def test_invalid_credentials_fail_without_advancing_token(self):
        self.ingest()
        self.client.routes["user"] = AuthenticationError(401)
        self.assertEqual(self.collector.run(), "failed")
        self.assertEqual(self.store.get("sync_token"), "next-token")
        self.assertFalse(healthy(self.store))

    def test_partial_coverage_and_interrupted_previous_run_are_visible(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO runs(started_at,status) VALUES('old','running')")
        self.client.routes["backups"] = APIError(403)
        self.assertEqual(self.collector.run(), "partial")
        self.assertEqual(self.store.status()["runs"][1]["status"], "interrupted")
        self.assertIn("MFA", next(c["detail"] for c in self.store.status()["coverage"] if c["source"] == "native_backups"))

    def test_run_budget_keeps_progress_and_resume_state(self):
        self.client.routes["tasks/completed/stats"] = BudgetReached()
        self.assertEqual(self.collector.run(), "backfill_in_progress")
        self.assertEqual(self.store.get("sync_token"), "next-token")

    def test_health_staleness_and_integrity(self):
        self.assertEqual(self.collector.run(), "complete")
        self.assertTrue(healthy(self.store))
        with self.store.db:
            self.store.set("last_progress", (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat())
        self.assertFalse(healthy(self.store))

    def test_duplicate_backup_is_locked(self):
        with exclusive_lock(self.root):
            with self.assertRaises(RuntimeError):
                with exclusive_lock(self.root):
                    pass


class TransportTests(unittest.TestCase):
    def response(self, status=200, data=None, headers=None):
        response = Mock(status_code=status, headers=headers or {})
        response.json.return_value = data if data is not None else {"results": [], "next_cursor": None}
        return response

    def test_sync_request_has_no_commands_and_gets_all_resources(self):
        session = Mock()
        session.request.return_value = self.response(data={"sync_token": "new"})
        client = Client("private-token", session=session)
        client.read("sync", sync_token="*")
        args, kwargs = session.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(kwargs["data"], {"sync_token": "*", "resource_types": '["all"]'})
        self.assertFalse(kwargs["allow_redirects"])

    def test_pagination_uses_parameter_dependent_cursor(self):
        session = Mock()
        session.request.side_effect = [self.response(data={"items": [{"id": "a"}], "next_cursor": "next"}),
                                       self.response(data={"items": [{"id": "b"}], "next_cursor": None})]
        pages = list(Client("token", session=session).pages("tasks/completed/by_completion_date", {"since": "date"}))
        self.assertEqual(len(pages), 2)
        self.assertEqual(session.request.call_args.kwargs["params"]["cursor"], "next")
        self.assertEqual(session.request.call_args.kwargs["params"]["since"], "date")

    def test_rate_limit_obeys_retry_after(self):
        session = Mock()
        session.request.side_effect = [self.response(429, headers={"Retry-After": "7"}), self.response()]
        sleep = Mock()
        Client("token", session=session, sleep=sleep).read("projects")
        sleep.assert_called_once_with(7)

    def test_long_retry_after_defers_without_early_retry(self):
        session = Mock()
        session.request.return_value = self.response(429, headers={"Retry-After": "120"})
        with self.assertRaises(APIError):
            Client("token", session=session, sleep=Mock()).read("projects")
        self.assertEqual(session.request.call_count, 1)

    def test_invalid_credentials_never_retry_or_expose_token(self):
        session = Mock()
        session.request.return_value = self.response(401)
        with self.assertRaises(AuthenticationError) as caught:
            Client("secret-value", session=session).read("user")
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertEqual(session.request.call_count, 1)

    def test_backup_mfa_does_not_masquerade_as_invalid_account_credentials(self):
        session = Mock()
        session.request.side_effect = [self.response(401), self.response(data={"id": "user-1"})]
        with self.assertRaises(APIError) as caught:
            Client("secret-value", session=session).read("backups")
        self.assertNotIsInstance(caught.exception, AuthenticationError)
        self.assertEqual(caught.exception.tag, "backup_mfa_required")
        self.assertNotIn("secret-value", str(caught.exception))

    def test_backup_credential_recheck_propagates_real_revocation(self):
        session = Mock()
        session.request.return_value = self.response(401)
        with self.assertRaises(AuthenticationError):
            Client("secret-value", session=session).read("backups")

    def test_bounded_server_retries(self):
        session = Mock()
        session.request.return_value = self.response(503)
        with self.assertRaises(APIError):
            Client("token", session=session, sleep=Mock()).read("projects")
        self.assertEqual(session.request.call_count, 4)

    def test_private_downloads_and_non_https_are_rejected(self):
        with self.assertRaises(APIError):
            public_url("http://example.org/file")
        with patch("api.socket.getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 443))]):
            with self.assertRaises(APIError):
                public_url("https://localhost/file")

    def test_archive_redirect_does_not_forward_credentials_to_cdn(self):
        session = Mock()
        final = self.response()
        final.iter_content.return_value = [b"abc"]
        session.request.side_effect = [self.response(302, headers={"Location": "https://cdn.example.org/file"}), final]
        with tempfile.TemporaryDirectory() as root, patch("api.public_url"):
            Client("secret", session=session).download("https://api.todoist.com/api/v1/backups/download?file=demo.zip",
                                                       Path(root) / "file", archive=True)
        self.assertIn("Authorization", session.request.call_args_list[0].kwargs["headers"])
        self.assertEqual(session.request.call_args_list[1].kwargs["headers"], {})

    def test_pagination_unknown_shape_and_repeated_cursor_fail(self):
        with self.assertRaises(APIError):
            page_items({"unexpected": []})
        session = Mock()
        session.request.return_value = self.response(data={"results": [], "next_cursor": "repeat"})
        with self.assertRaises(APIError):
            list(Client("token", session=session).pages("projects"))

    def test_only_relative_paths_and_command_free_sync_allowed(self):
        client = Client("token", session=Mock())
        with self.assertRaises(ValueError):
            client.read("https://other.example")
        with self.assertRaises(ValueError):
            client.read("sync", params={"commands": []}, sync_token="*")


class EntrypointTests(unittest.TestCase):
    def test_initial_failure_still_schedules_and_logs_in_amsterdam(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            binaries = root / "bin"
            binaries.mkdir()
            for name, script in {"python": "#!/bin/sh\nexit 1\n", "supercronic": "#!/bin/sh\ncat \"$1\"\n"}.items():
                path = binaries / name
                path.write_text(script)
                path.chmod(0o755)
            env = dict(os.environ, PATH=f"{binaries}:{os.environ['PATH']}", TODOIST_API_TOKEN="hidden",
                       TODOIST_DATA_DIR=str(root / "data"), TODOIST_BACKUP_DIR=str(root / "backup"), LOG_DIR=str(root / "logs"),
                       TODOIST_CRONTAB_FILE=str(root / "schedule"))
            result = subprocess.run(["bash", str(Path(__file__).with_name("entrypoint.sh"))], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("0 */4 * * *", result.stdout)
            self.assertIn("Europe/Amsterdam", result.stdout)
            self.assertNotIn("hidden", result.stdout)
            self.assertTrue((root / "logs/todoist-backup.log").is_file())


if __name__ == "__main__":
    unittest.main()
