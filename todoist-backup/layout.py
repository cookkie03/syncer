"""Preserving migration and atomic, unlimited-retention backup publication."""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def migrate(legacy, state, backup):
    legacy, state, backup = map(Path, (legacy, state, backup))
    marker = state / 'storage-migration.json'
    if legacy.is_symlink():
        raise RuntimeError("Legacy directory symlink requires inspection")
    if marker.exists() or not legacy.exists() or legacy.resolve() == state.resolve():
        return
    mappings = []
    for source in legacy.rglob('*'):
        if not source.is_file() or source.name == 'backup.lock':
            continue
        if source.is_symlink():
            raise RuntimeError('Legacy symlinks require inspection')
        relative = source.relative_to(legacy)
        target = backup / relative if relative.parts[0] in ('objects', 'exports') else state / relative
        if target.exists() and digest(source) != digest(target):
            raise RuntimeError('Migration conflict; existing data preserved')
        mappings.append((source, target))
    if not mappings:
        return
    # Preflight every conflict before copying. Sources remain untouched for recovery.
    for source, target in mappings:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_name(target.name + '.migration.tmp')
            shutil.copyfile(source, temporary)
            if digest(source) != digest(temporary):
                raise RuntimeError('Migration checksum mismatch')
            os.replace(temporary, target)
    marker.write_text(json.dumps({'source': str(legacy), 'files': len(mappings)}))


def publish(store, root):
    root = Path(root)
    snapshots = root / 'snapshots'
    snapshots.mkdir(parents=True, exist_ok=True)
    current = root / 'current'
    if current.exists() and not current.is_symlink():
        raise RuntimeError('Physical current directory requires inspection')
    name = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S.%fZ')
    snapshot = snapshots / name
    staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=root))
    try:
        destination = sqlite3.connect(staging / 'todoist.sqlite3')
        try:
            store.db.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Snapshot database integrity failure')
        finally:
            destination.close()
        exported = store.export(staging, reference_dir=snapshot)
        exported = Path(exported)
        for path in exported.iterdir():
            path.rename(staging / path.name)
        exported.rmdir()
        manifest = {'status': store.status(), 'checksums': {
            path.name: digest(path) for path in staging.iterdir() if path.is_file()}}
        (staging / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        for path in staging.iterdir():
            if path.is_file():
                with path.open('rb') as handle:
                    os.fsync(handle.fileno())
        staging.rename(snapshot)
        pointer = root / '.current.tmp'
        if pointer.exists() and not pointer.is_symlink():
            raise RuntimeError('Unexpected publication pointer')
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(snapshot.relative_to(root), target_is_directory=True)
        os.replace(pointer, current)
        return snapshot
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def verify_snapshot(root):
    root = Path(root)
    current = root / 'current'
    if not current.is_symlink() or not current.exists():
        return ['Missing published snapshot']
    errors = []
    for snapshot in sorted((root / 'snapshots').iterdir()):
        if not snapshot.is_dir():
            continue
        try:
            manifest = json.loads((snapshot / 'manifest.json').read_text())
            for name, expected in manifest['checksums'].items():
                if Path(name).name != name or not (snapshot / name).is_file() or digest(snapshot / name) != expected:
                    errors.append('Snapshot checksum mismatch: ' + snapshot.name + '/' + name)
        except (OSError, ValueError, KeyError, TypeError):
            errors.append('Unreadable snapshot manifest: ' + snapshot.name)
    return errors
