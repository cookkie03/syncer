#!/usr/bin/env python3
"""Preview or migrate legacy storage locally, with services stopped and no overwrites."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def migration_plan(root: Path) -> list[tuple[Path, Path]]:
    moves = []

    def move(source: str | Path, destination: str | Path) -> None:
        source, destination = root / source, root / destination
        if source.exists() or source.is_symlink():
            moves.append((source, destination))

    for folder in ("config", "token", "status"):
        move(f"vdirsyncer/{folder}", f"vdirsyncer/state/{folder}")
    move("caldav-backup/backup/latest", "caldav-backup/backup/current")
    for source in sorted((root / "caldav-backup/backup").glob("*.ics")):
        move(source, Path("caldav-backup/backup/snapshots/legacy-root") / source.name)
    move("caldav-backup/backup/manifest.json", "caldav-backup/backup/snapshots/legacy-root/manifest.json")
    move("google-contacts-backup/backup/latest", "google-contacts-backup/backup/current")
    move("google-contacts-backup/backup/latest.json", "google-contacts-backup/state/legacy-latest.json")
    for source in sorted((root / "google-contacts-backup/backup").glob("legacy-latest-*")):
        move(source, Path("google-contacts-backup/backup/snapshots") / source.name)
    move("google-contacts-backup/data", "google-contacts-backup/state/legacy-data")
    move("google-contacts-backup/state/backup", "google-contacts-backup/backup/snapshots/legacy-state-backup")

    move("spotify-backup/data/.cache", "spotify-backup/state/.cache")
    move("spotify-backup/data/backup/playlist_track_cache.json", "spotify-backup/state/playlist_track_cache.json")
    move("spotify-backup/data/backup/spotify_backup_current.json",
         "spotify-backup/backup/current/spotify_backup_current.json")
    move("spotify-backup/data/backup/latest", "spotify-backup/backup/snapshots/legacy-latest-spotify")
    old_snapshots = root / "spotify-backup/data/backup/snapshots"
    if old_snapshots.is_dir():
        for source in sorted(old_snapshots.iterdir()):
            if source.is_file() and source.name.endswith("-spotify_backup_current.json"):
                name = source.name[:-len("-spotify_backup_current.json")] + "Z"
                move(source, Path("spotify-backup/backup/snapshots") / name / "spotify_backup_current.json")
            else:
                move(source, Path("spotify-backup/backup/snapshots") / source.name)
    old_backup = root / "spotify-backup/data/backup"
    if old_backup.is_dir():
        for source in sorted(old_backup.iterdir()):
            if source.name not in {"snapshots", "latest", "spotify_backup_current.json", "playlist_track_cache.json"}:
                move(source, Path("spotify-backup/backup/snapshots/legacy-extra-backup") / source.name)
    old_data = root / "spotify-backup/data"
    if old_data.is_dir():
        for source in sorted(old_data.iterdir()):
            if source.name not in {"backup", ".cache"}:
                move(source, Path("spotify-backup/state/legacy-data") / source.name)

    for folder in ("json", "zip_exports"):
        move(f"notion-backup/backup/{folder}", f"notion-backup/backup/current/{folder}")
    move("notion-backup/backup/.last_notified_msg_id", "notion-backup/state/.last_notified_msg_id")
    move("vtodo-notion/state/logs", "vtodo-notion/logs/legacy-state-logs")
    return moves


def validate_plan(root: Path, moves: list[tuple[Path, Path]]) -> None:
    destinations = set()
    for source, destination in moves:
        # Never move a directory through a symlink to another location.
        for path in (source.parent, destination.parent):
            for parent in (path, *path.parents):
                if parent == root:
                    break
                if parent.is_symlink():
                    raise RuntimeError(f"Symlink parent requires manual review: {parent.relative_to(root)}")
                if parent.exists() and not parent.is_dir():
                    raise RuntimeError(f"Destination parent is not a directory: {parent.relative_to(root)}")
        if destination.exists() or destination.is_symlink() or destination in destinations:
            raise RuntimeError(f"Destination already exists; nothing overwritten: {destination.relative_to(root)}")
        destinations.add(destination)
        if source.is_symlink():
            # Only current pointers inside the backup tree are portable.
            target = Path(os.readlink(source))
            try:
                source.resolve().relative_to(source.parent.resolve())
            except ValueError:
                raise RuntimeError(f"External link requires manual review: {source.relative_to(root)}")
            if target.is_absolute():
                raise RuntimeError(f"Nonportable link requires manual review: {source.relative_to(root)}")
            if not source.exists():
                raise RuntimeError(f"Broken current pointer: {source.relative_to(root)}")


def apply_plan(root: Path, moves: list[tuple[Path, Path]]) -> None:
    validate_plan(root, moves)
    for source, destination in moves:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
    # Only remove old containers for files when actually empty.
    for folder in ("spotify-backup/data/backup/snapshots", "spotify-backup/data/backup", "spotify-backup/data"):
        path = root / folder
        if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
            path.rmdir()


def ensure_stopped(root: Path) -> None:
    if not shutil.which("docker"):
        return  # PC without Docker: local file preparation only.
    result = subprocess.run(["docker", "compose", "ps", "--status", "running", "-q"],
                            cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Cannot verify Docker state. Run with Docker available and services stopped.")
    if result.stdout.strip():
        raise RuntimeError("Stop services with 'docker compose stop' before migrating storage.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Move files; default only previews")
    args = parser.parse_args()
    try:
        moves = migration_plan(ROOT)
        validate_plan(ROOT, moves)
        for source, destination in moves:
            print(f"{source.relative_to(ROOT)} -> {destination.relative_to(ROOT)}")
        if args.apply and moves:
            ensure_stopped(ROOT)
            apply_plan(ROOT, moves)
        print("OK: storage migrated" if args.apply else "Preview only; use --apply with services stopped")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
