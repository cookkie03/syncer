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

    latest = backup_root / "latest"
    manifest = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
    assert result == latest
    assert (latest / "calendar_home.ics").read_text(encoding="utf-8").startswith("BEGIN")
    assert manifest["checksums"]["calendar_home.ics"] == expected_checksum


def test_failed_run_does_not_replace_latest(tmp_path):
    backup_root = tmp_path / "backup"
    latest = backup_root / "latest"
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
    latest = backup_root / "latest"
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

    backup.prune_snapshots(backup_root, retention=2)

    assert sorted(path.name for path in snapshots.iterdir()) == [
        "2026-09-09T000000Z",
        "2026-09-10T000000Z",
    ]
