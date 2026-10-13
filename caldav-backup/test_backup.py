import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

pytest.importorskip("caldav")

os.environ.setdefault("CALDAV_URL", "https://example.invalid")
os.environ.setdefault("CALDAV_USERNAME", "test")
os.environ.setdefault("CALDAV_PASSWORD", "test")

import backup
from backup_storage import prune_snapshots


def test_mixed_collection_read_failure_is_fatal():
    class Collection:
        name = "Mixed"
        url = "https://example.invalid/mixed/"
        def todos(self, **kwargs):
            raise RuntimeError("502 Bad Gateway")
        def events(self):
            return []
    principal = type("Principal", (), {"calendars": lambda self: [Collection()]})()
    client = type("Client", (), {"principal": lambda self: principal})()
    with pytest.raises(RuntimeError, match="Could not read collection"):
        backup.discover_all_calendars(client)


def test_colliding_collection_names_cannot_overwrite_files(tmp_path):
    backup.export_calendar([], "A/B", tmp_path)
    with pytest.raises(FileExistsError):
        backup.export_calendar([], "A:B", tmp_path)


def test_502_retries_and_preserves_current(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(backup, "RETRY_ATTEMPTS", 3)
    previous = tmp_path / "current"
    previous.mkdir()
    (previous / "keep.ics").write_text("previous")
    attempts, sleeps = [], []
    def fail():
        attempts.append(1)
        raise RuntimeError("502 Bad Gateway <html>private response</html>")
    monkeypatch.setattr(backup, "run_backup", fail)
    monkeypatch.setattr(backup.time, "sleep", sleeps.append)
    assert backup.backup_with_retries() is False
    assert len(attempts) == 3
    assert sleeps == [15, 30]
    assert (previous / "keep.ics").read_text() == "previous"
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["result"] == "failed"
    assert "private response" not in status["error"]


def test_authorization_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    attempts = []
    def fail():
        attempts.append(1)
        raise backup.caldav.lib.error.AuthorizationError("unauthorized")
    monkeypatch.setattr(backup, "run_backup", fail)
    assert backup.backup_with_retries() is False
    assert len(attempts) == 1


def test_watch_pauses_authentication_until_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["backup.py", "--watch", "--interval", "14400"])
    attempts, sleeps = [], []
    def fail():
        attempts.append(1)
        raise backup.caldav.lib.error.AuthorizationError("unauthorized")
    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt()
    monkeypatch.setattr(backup, "run_backup", fail)
    monkeypatch.setattr(backup.time, "sleep", sleep)
    backup.main()
    assert len(attempts) == 1
    assert len(sleeps) == 2
    assert json.loads((tmp_path / "status.json").read_text())["requires_intervention"] is True


def test_health_rejects_corruption_and_stale_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    current = tmp_path / "current"
    current.mkdir()
    payload = current / "calendar_home.ics"
    payload.write_text("BEGIN:VCALENDAR\nEND:VCALENDAR\n")
    manifest = {"timestamp": backup.datetime.now(backup.timezone.utc).isoformat(),
                "checksums": {payload.name: backup.sha256_file(payload)}}
    path = current / "manifest.json"
    path.write_text(json.dumps(manifest))
    backup.check_health()
    payload.write_text("corrupt")
    with pytest.raises(RuntimeError, match="checksum"):
        backup.check_health()
    manifest["checksums"][payload.name] = backup.sha256_file(payload)
    manifest["timestamp"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="too old"):
        backup.check_health()


def test_health_rejects_latest_failed_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    current = tmp_path / "current"
    current.mkdir()
    payload = current / "calendar_home.ics"
    payload.write_text("valid")
    (current / "manifest.json").write_text(json.dumps({
        "timestamp": backup.datetime.now(backup.timezone.utc).isoformat(),
        "checksums": {payload.name: backup.sha256_file(payload)}}))
    (tmp_path / "status.json").write_text(json.dumps({"result": "failed"}))
    with pytest.raises(RuntimeError, match="attempt failed"):
        backup.check_health()


def test_empty_collections_are_valid_ics():
    assert backup.build_ics_from_vevents([]).endswith("END:VCALENDAR\n")
    assert backup.build_ics_from_vtodos([]).endswith("END:VCALENDAR\n")


def test_malformed_item_cannot_be_silently_omitted():
    with pytest.raises(RuntimeError, match="complete VEVENT"):
        backup.build_ics_from_vevents([type("Event", (), {"data": "bad"})()])
    with pytest.raises(RuntimeError, match="complete VTODO"):
        backup.build_ics_from_vtodos([type("Todo", (), {"data": "bad"})()])


def test_all_event_components_and_timezone_are_preserved():
    raw = (
        "BEGIN:VCALENDAR\n"
        "BEGIN:VTIMEZONE\nTZID:Europe/Amsterdam\nEND:VTIMEZONE\n"
        "BEGIN:VEVENT\nUID:one\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:two\nEND:VEVENT\n"
        "END:VCALENDAR\n"
    )
    result = backup.build_ics_from_vevents([type("Event", (), {"data": raw})()])
    assert result.count("BEGIN:VEVENT") == 2
    assert result.count("BEGIN:VTIMEZONE") == 1


def test_promote_snapshot_writes_manifest_checksums_and_latest(tmp_path, monkeypatch):
    backup_root = tmp_path / "backup"
    staging = backup_root / ".staging" / "run-1"
    staging.mkdir(parents=True)
    payload = staging / "calendar_home.ics"
    payload.write_text("BEGIN:VCALENDAR\nEND:VCALENDAR\n", encoding="utf-8")
    expected_checksum = hashlib.sha256(payload.read_bytes()).hexdigest()

    monkeypatch.setenv("CALDAV_BACKUP_RETENTION", "2")
    result = backup.promote_snapshot(staging, backup_root, {"events": 0})

    latest = backup_root / "current"
    manifest = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
    assert result == latest
    assert (latest / "calendar_home.ics").read_text(encoding="utf-8").startswith("BEGIN")
    assert manifest["checksums"]["calendar_home.ics"] == expected_checksum


def test_failed_run_does_not_replace_latest(tmp_path):
    backup_root = tmp_path / "backup"
    latest = backup_root / "current"
    latest.mkdir(parents=True)
    marker = latest / "marker.txt"
    marker.write_text("previous", encoding="utf-8")
    staging = backup_root / ".staging" / "run-2"
    staging.mkdir(parents=True)

    with pytest.raises(RuntimeError):
        backup.promote_snapshot(staging, backup_root, {"failed": True})

    assert marker.read_text(encoding="utf-8") == "previous"


def test_missing_previous_collection_keeps_latest(tmp_path):
    backup_root = tmp_path / "backup"
    latest = backup_root / "current"
    latest.mkdir(parents=True)
    previous = latest / "calendar_home.ics"
    previous.write_text("previous", encoding="utf-8")
    staging = backup_root / ".staging" / "run-3"
    staging.mkdir(parents=True)
    (staging / "tasks_home.ics").write_text("new", encoding="utf-8")

    with pytest.raises(RuntimeError, match="missing previous collections"):
        backup.promote_snapshot(staging, backup_root, {})

    assert previous.read_text(encoding="utf-8") == "previous"


def test_retention_keeps_newest_snapshots(tmp_path):
    backup_root = tmp_path / "backup"
    snapshots = backup_root / "snapshots"
    snapshots.mkdir(parents=True)
    for name in ("2026-09-08T000000Z", "2026-09-09T000000Z", "2026-09-10T000000Z"):
        (snapshots / name).mkdir()

    prune_snapshots(backup_root, retention=2)

    assert sorted(path.name for path in snapshots.iterdir()) == [
        "2026-09-09T000000Z",
        "2026-09-10T000000Z",
    ]
