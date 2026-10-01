"""Transactional metadata, immutable payloads/files, and inspectable exports."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SECRET_KEYS = {"token", "api_token", "access_token", "refresh_token", "sync_token", "client_secret", "password", "websocket_url"}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k.lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def canonical(value):
    return json.dumps(clean(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payloads(hash TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records(
 collection TEXT NOT NULL, id TEXT NOT NULL, hash TEXT NOT NULL REFERENCES payloads(hash),
 deleted INTEGER NOT NULL, availability TEXT NOT NULL, last_seen TEXT NOT NULL,
 PRIMARY KEY(collection,id));
CREATE TABLE IF NOT EXISTS revisions(
 seq INTEGER PRIMARY KEY, collection TEXT NOT NULL, id TEXT NOT NULL,
 hash TEXT NOT NULL REFERENCES payloads(hash), run_id INTEGER NOT NULL, captured_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS responses(
 seq INTEGER PRIMARY KEY, source TEXT NOT NULL, hash TEXT NOT NULL REFERENCES payloads(hash),
 run_id INTEGER NOT NULL, captured_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS coverage(
 source TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files(
 ref TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL, name TEXT NOT NULL,
 archive INTEGER NOT NULL, hash TEXT, size INTEGER, status TEXT NOT NULL, error TEXT);
CREATE INDEX IF NOT EXISTS revisions_record ON revisions(collection,id,seq);
"""


class Store:
    def __init__(self, root, *, readonly=False, object_root=None):
        self.root = Path(root)
        self.object_root = Path(object_root) if object_root is not None else self.root / "objects"
        self.path = self.root / "todoist.sqlite3"
        if readonly:
            self.db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path)
            self.db.executescript(SCHEMA)
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version','1')")
            self.db.commit()
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, json.dumps(value)))

    def progress(self):
        self.set("last_progress", utcnow())

    def payload(self, value):
        text = canonical(value)
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO payloads VALUES(?,?)", (digest, text))
        return digest

    def response(self, source, data, run_id):
        self.db.execute("INSERT INTO responses(source,hash,run_id,captured_at) VALUES(?,?,?,?)",
                        (source, self.payload(data), run_id, utcnow()))

    def put(self, collection, value, run_id, *, record_id=None):
        if not isinstance(value, dict):
            raise ValueError("Resource record must be an object")
        identifier = str(record_id if record_id is not None else value.get("id", ""))
        if not identifier:
            # Preserve resources without an ID as one collection-level object.
            identifier = "singleton"
        old = self.db.execute("SELECT hash FROM records WHERE collection=? AND id=?",
                              (collection, identifier)).fetchone()
        digest = self.payload(value)
        deleted = bool(value.get("is_deleted"))
        self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?)",
                        (collection, identifier, digest, deleted, "deleted" if deleted else "accessible", utcnow()))
        if not old or old[0] != digest:
            self.db.execute("INSERT INTO revisions(collection,id,hash,run_id,captured_at) VALUES(?,?,?,?,?)",
                            (collection, identifier, digest, run_id, utcnow()))
        self._attachments(f"{collection}:{identifier}", value)

    def _attachments(self, source, value):
        if isinstance(value, dict):
            url = value.get("file_url")
            if isinstance(url, str) and url:
                self.queue_file(source, url, value.get("file_name", "attachment"))
            for child in value.values():
                self._attachments(source, child)
        elif isinstance(value, list):
            for child in value:
                self._attachments(source, child)

    def queue_file(self, source, url, name, archive=False):
        ref = hashlib.sha256(f"{source}\n{url}".encode()).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO files(ref,source,url,name,archive,status) VALUES(?,?,?,?,?,'pending')",
                        (ref, source, url, str(name), archive))

    def records(self, collection):
        rows = self.db.execute("SELECT r.id,p.json,r.deleted,r.availability FROM records r JOIN payloads p ON p.hash=r.hash WHERE collection=?", (collection,))
        return [(row["id"], json.loads(row["json"]), row["deleted"], row["availability"]) for row in rows]

    def cover(self, source, status, detail=""):
        self.db.execute("INSERT OR REPLACE INTO coverage VALUES(?,?,?,?)", (source, status, detail, utcnow()))

    def missing(self, collection, identifier):
        self.db.execute("UPDATE records SET availability='inaccessible' WHERE collection=? AND id=? AND deleted=0",
                        (collection, identifier))

    def ingest_sync(self, data, run_id):
        if not isinstance(data, dict) or not isinstance(data.get("sync_token"), str) or not data["sync_token"]:
            raise ValueError("Sync response has no usable token")
        with self.db:
            user = data.get("user")
            identity = self.get("account_id")
            if isinstance(user, dict) and user.get("id") is not None:
                incoming = str(user["id"])
                if identity is not None and incoming != identity:
                    raise ValueError("Account identity mismatch; existing backup preserved")
                self.set("account_id", incoming)
            elif identity is None:
                raise ValueError("Initial sync response has no account identity")
            self.response("sync", data, run_id)
            if data.get("full_sync"):
                for collection in ("items", "projects", "sections", "labels", "notes", "project_notes", "filters", "reminders", "locations"):
                    if collection in data and isinstance(data[collection], list):
                        self.db.execute("UPDATE records SET availability='absent_from_active_sync' WHERE collection=? AND deleted=0", (collection,))
            for collection, values in data.items():
                if collection in {"sync_token", "full_sync", "full_sync_date_utc", "temp_id_mapping", "sync_status"}:
                    continue
                if values is None:
                    continue
                if isinstance(values, list):
                    for value in values:
                        if not isinstance(value, dict):
                            continue
                        identifier = value.get("id")
                        if collection == "workspace_users":
                            identifier = f"{value.get('workspace_id')}:{value.get('user_id')}"
                        if identifier is None:
                            identifier = hashlib.sha256(canonical(value).encode()).hexdigest()
                        self.put(collection, value, run_id, record_id=identifier)
                elif isinstance(values, dict):
                    self.put(collection, values, run_id, record_id="singleton")
                else:
                    self.put(collection, {"value": values}, run_id, record_id="singleton")
            self.set("sync_token", data["sync_token"])
            self.set("last_sync", utcnow())
            self.progress()
            self.cover("sync", "complete")

    def install_file(self, row, temp_path):
        digest = file_hash(temp_path)
        destination = self.object_root / digest[:2] / digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = Path(temp_path).stat().st_size
        # Existing identical files are reused; a corrupt object is repaired from the new download.
        if not destination.exists() or file_hash(destination) != digest:
            os.replace(temp_path, destination)
        with self.db:
            self.db.execute("UPDATE files SET hash=?,size=?,status='complete',error=NULL WHERE ref=?",
                            (digest, size, row["ref"]))
            self.progress()

    def verify(self, *, quick=False):
        errors = []
        result = self.db.execute("PRAGMA quick_check" if quick else "PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            errors.append("SQLite integrity check failed")
        if list(self.db.execute("PRAGMA foreign_key_check")):
            errors.append("SQLite references are inconsistent")
        if not quick:
            for row in self.db.execute("SELECT hash,json FROM payloads"):
                if hashlib.sha256(row["json"].encode()).hexdigest() != row["hash"]:
                    errors.append(f"Corrupt payload: {row['hash']}")
        for row in self.db.execute("SELECT DISTINCT hash,size FROM files WHERE status='complete'"):
            path = self.object_root / row["hash"][:2] / row["hash"]
            if not path.is_file() or path.stat().st_size != row["size"]:
                errors.append(f"Missing or truncated object: {row['hash']}")
            elif not quick and file_hash(path) != row["hash"]:
                errors.append(f"Corrupt object: {row['hash']}")
        return errors

    def status(self):
        runs = [dict(row) for row in self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5")]
        return {
            "account_id": self.get("account_id"), "last_progress": self.get("last_progress"),
            "last_sync": self.get("last_sync"), "history_next": self.get("history_next"),
            "history_target": self.get("history_target"), "history_complete": self.get("history_complete", False),
            "activity_history_next": self.get("activity_history_next"),
            "counts": {r[0]: r[1] for r in self.db.execute("SELECT collection,count(*) FROM records GROUP BY collection")},
            "revisions": self.db.execute("SELECT count(*) FROM revisions").fetchone()[0],
            "files": {r[0]: r[1] for r in self.db.execute("SELECT status,count(*) FROM files GROUP BY status")},
            "coverage": [dict(r) for r in self.db.execute("SELECT * FROM coverage ORDER BY source")], "runs": runs,
            "limitations": ["Polling captures observed revisions, not every intermediate edit.",
                             "Historical availability depends on Todoist access and account plan.",
                             "Credentials are excluded from saved payloads and exports."],
        }

    def export(self, destination, *, reference_dir=None):
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        # A new generation avoids readers seeing half an export, and never replaces prior exports.
        output = Path(tempfile.mkdtemp(prefix="todoist-export-", dir=destination))
        with self.db:
            self.db.execute("BEGIN")
            def payload_record(row):
                record = dict(row)
                record["payload"] = json.loads(record.pop("json"))
                return record

            def file_records():
                for row in self.db.execute("SELECT * FROM files ORDER BY ref"):
                    record = dict(row)
                    if row["hash"]:
                        path = self.object_root / row["hash"][:2] / row["hash"]
                        record["object_path"] = str(Path("objects") / row["hash"][:2] / row["hash"])
                        record["export_relative_path"] = os.path.relpath(path.resolve(), Path(reference_dir or output).resolve())
                        record["local_path"] = str(path.resolve())
                    yield record

            def write_array(stream, records):
                stream.write("[\n")
                for index, record in enumerate(records):
                    if index:
                        stream.write(",\n")
                    json.dump(record, stream, ensure_ascii=False, indent=2)
                stream.write("\n]")

            # Stream indefinite history instead of loading it all into NAS memory.
            with (output / "account.json").open("w", encoding="utf-8") as stream:
                stream.write('{\n"status": ')
                json.dump(self.status(), stream, ensure_ascii=False, indent=2)
                stream.write(',\n"records": {\n')
                collections = self.db.execute("SELECT DISTINCT collection FROM records ORDER BY collection")
                for index, row in enumerate(collections):
                    if index:
                        stream.write(",\n")
                    json.dump(row[0], stream)
                    stream.write(": ")
                    records = self.db.execute("SELECT r.*,p.json FROM records r JOIN payloads p ON r.hash=p.hash WHERE collection=? ORDER BY id", (row[0],))
                    write_array(stream, (payload_record(r) for r in records))
                stream.write("\n}")
                for table in ("revisions", "responses"):
                    stream.write(f',\n"{table}": ')
                    records = self.db.execute(f"SELECT r.*,p.json FROM {table} r JOIN payloads p ON r.hash=p.hash ORDER BY seq")
                    write_array(stream, (payload_record(r) for r in records))
                stream.write(',\n"files": ')
                write_array(stream, file_records())
                stream.write("\n}\n")
            with (output / "tasks.csv").open("w", encoding="utf-8", newline="") as stream:
                fields = ["collection", "id", "content", "description", "project_id", "section_id", "parent_id",
                          "priority", "due", "deadline", "labels", "completed_at", "deleted", "availability"]
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for collection in ("items", "completed_tasks"):
                    records = self.db.execute("SELECT r.*,p.json FROM records r JOIN payloads p ON r.hash=p.hash WHERE collection=? ORDER BY id", (collection,))
                    for record in records:
                        payload = json.loads(record["json"])
                        values = {k: payload.get(k, "") for k in fields}
                        values.update({k: record[k] for k in ("id", "deleted", "availability")})
                        values["collection"] = collection
                        for key, value in values.items():
                            if isinstance(value, (dict, list)):
                                values[key] = json.dumps(value, ensure_ascii=False)
                            elif isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                                values[key] = "'" + value
                        writer.writerow(values)
            (output / "COMPLETE").write_text(utcnow(), encoding="utf-8")
        return output
