"""Publish complete snapshots and expose one current backup, shared by exporters."""

import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def new_staging(backup_root: Path) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".staging-", dir=backup_root))


def point_current(backup_root: Path, snapshot: Path) -> Path:
    """Replace the pointer atomically; preserve a physical current from migration."""
    current = backup_root / "current"
    pointer = backup_root / ".current.tmp"
    if pointer.exists() and not pointer.is_symlink():
        raise FileExistsError(pointer)
    archive = None
    if current.exists() and not current.is_symlink():
        archive = backup_root / "snapshots" / ("legacy-current-" + snapshot.name)
        if archive.exists():
            raise FileExistsError(archive)
        current.rename(archive)
    try:
        if pointer.is_symlink():
            pointer.unlink()
        pointer.symlink_to(snapshot.relative_to(backup_root), target_is_directory=True)
        os.replace(pointer, current)
    except Exception:
        if archive is not None and not current.exists():
            archive.rename(current)
        raise
    return current


def prune_snapshots(backup_root: Path, retention: int) -> None:
    """Keep the current snapshot and at least retention complete dated snapshots.

    Legacy archives and unfinished runs are never removed by this policy.
    """
    root = backup_root / "snapshots"
    current = (backup_root / "current").resolve()
    snapshots = sorted(
        (p for p in root.iterdir() if p.is_dir() and not p.is_symlink()
         and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{6}(?:\d{6})?Z|\d{8}_\d{6}", p.name)),
        key=lambda p: re.sub(r"\D", "", p.name), reverse=True,
    ) if root.exists() else []
    retained = [p for p in snapshots if p.resolve() == current]
    retained += [p for p in snapshots if p.resolve() != current][:max(1, retention) - len(retained)]
    for snapshot in snapshots:
        if snapshot not in retained:
            shutil.rmtree(snapshot)


def publish_snapshot(staging: Path, backup_root: Path, retention: int,
                     name: str | None = None) -> Path:
    """Publish only after the caller has fully written and validated staging."""
    snapshots = backup_root / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    name = name or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S%fZ")
    if name in {"", ".", ".."} or Path(name).name != name:
        raise ValueError("Snapshot name must be a single directory name")
    snapshot = snapshots / name
    if snapshot.exists() or snapshot.is_symlink():
        raise FileExistsError(snapshot)
    staging.rename(snapshot)
    current = point_current(backup_root, snapshot)
    prune_snapshots(backup_root, retention)
    return current
