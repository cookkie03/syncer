#!/usr/bin/env python3
"""Headless, read-only Notion API exporter with immutable versioned snapshots."""
from __future__ import annotations

import argparse
import csv
from functools import wraps
import fcntl
import hashlib
import html
import io
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from urllib.parse import urlparse, quote, unquote
from uuid import UUID

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backup_storage import new_staging, point_current, prune_snapshots

log = logging.getLogger("notion-backup")
VERSION = "2026-03-11"


def uid(value):
    return str(UUID(str(value)))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def title(obj):
    rich = obj.get("title", [])
    for prop in (obj.get("properties", {}).values() if obj.get("object") == "page" else []):
        if prop.get("type") == "title":
            rich = prop.get("title", [])
            break
    return "".join(x.get("plain_text", x.get("text", {}).get("content", "")) for x in rich) or obj.get("name") or "Untitled"


def cell(prop):
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in {"title", "rich_text"}:
        return "".join(x.get("plain_text", x.get("text", {}).get("content", "")) for x in value or [])
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


class DiskMap(MutableMapping):
    """Keep heavy per-page content on disk; load only the requested page."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.keys_index = {}

    def __getitem__(self, key):
        if key not in self.keys_index:
            raise KeyError(key)
        with (self.root / (key + '.json')).open(encoding='utf-8') as stream:
            return json.load(stream)

    def __setitem__(self, key, value):
        key = uid(key)
        temporary = self.root / (key + '.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
        temporary.replace(self.root / (key + '.json'))
        self.keys_index[key] = None

    def __delitem__(self, key):
        if key not in self.keys_index:
            raise KeyError(key)
        (self.root / (key + '.json')).unlink()
        del self.keys_index[key]

    def __iter__(self):
        return iter(self.keys_index)

    def __len__(self):
        return len(self.keys_index)


def trashed(obj):
    return any(obj.get(key) is True for key in ("in_trash", "is_archived", "archived"))


class NotionHTTPError(RuntimeError):
    def __init__(self, status, path):
        self.status = status
        super().__init__(f"Notion HTTP {status} at {path}")


def live_resource(kind):
    """Skip confirmed deletions, including deletion while reading a resource."""
    def decorate(read):
        @wraps(read)
        def wrapped(self, oid, *args, **kwargs):
            oid = uid(oid)
            if oid in self.excluded:
                return
            try:
                return read(self, oid, *args, **kwargs)
            except NotionHTTPError as exc:
                if exc.status != 404 or not self.deleted(kind, oid, refresh=True):
                    raise
                self.exclude(oid)
        return wrapped
    return decorate


class API:
    def __init__(self, token, *, delay=None, attempts=5):
        if not token:
            raise RuntimeError("Configure NOTION_PAT or NOTION_TOKEN locally")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + token, "Notion-Version": VERSION})
        delay = float(os.environ.get('NOTION_API_DELAY_SECONDS', '0.75')) if delay is None else delay
        if not math.isfinite(delay) or not 0 <= delay <= 60:
            raise ValueError('NOTION_API_DELAY_SECONDS must be finite and between 0 and 60')
        self.delay, self.attempts, self.last = delay, attempts, 0.0
        self.calls = 0
        self.max_requests = int(os.environ.get("NOTION_MAX_REQUESTS", "20000"))

    def request(self, method, path, **kwargs):
        for attempt in range(self.attempts):
            self.calls += 1
            if self.calls > self.max_requests:
                raise RuntimeError("NOTION_MAX_REQUESTS exceeded; nothing published")
            time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                response = self.session.request(method, "https://api.notion.com/v1" + path,
                                                timeout=(10, 60), **kwargs)
            except requests.RequestException:
                if attempt + 1 == self.attempts:
                    raise RuntimeError("Notion connection failed after retries") from None
                time.sleep(min(30, 2 ** attempt))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 == self.attempts:
                    raise RuntimeError(f"Notion HTTP {response.status_code} after retries")
                try:
                    delay = float(response.headers.get("Retry-After", 2 ** attempt))
                except ValueError:
                    delay = 2 ** attempt
                time.sleep(min(120, max(0, delay)))
                continue
            if not response.ok:
                # Never log request headers, signed download URLs, or raw server bodies.
                raise NotionHTTPError(response.status_code, path)
            return response.json()
        raise RuntimeError("Notion request failed")

    def pages(self, method, path, **values):
        cursor, seen = None, set()
        while True:
            payload = {**values, "page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = self.request(method, path, **{("params" if method == "GET" else "json"): payload})
            yield from data.get("results", [])
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
            if not cursor or cursor in seen:
                raise RuntimeError(f"Invalid pagination at {path}")
            seen.add(cursor)


class Crawl:
    def __init__(self, api, *, spool_dir=None):
        self.api = api
        self.objects, self.rows = {}, {}
        self.blocks = DiskMap(Path(spool_dir) / 'blocks') if spool_dir is not None else {}
        self.markdown = DiskMap(Path(spool_dir) / 'markdown') if spool_dir is not None else {}
        self.warnings = []
        self.excluded = set()
        self.metadata = {}

    def exclude(self, oid):
        if oid not in self.excluded:
            log.info("Skipping resource in Notion Trash: %s", oid)
        self.excluded.add(oid)
        for store in (self.objects, self.rows, self.blocks, self.markdown):
            if oid in store:
                del store[oid]

    def deleted(self, kind, oid, *, metadata=None, refresh=False, active=None):
        """A 404 alone is not proof of deletion: permissions can cause it too."""
        oid = uid(oid)
        active = set() if active is None else active
        if oid in active:
            raise RuntimeError("Cyclic Notion resource parents")
        if not refresh and oid in self.excluded:
            return True
        key = (kind, oid)
        if metadata is not None:
            obj = metadata
        elif not refresh and key in self.metadata:
            obj = self.metadata[key]
        else:
            endpoint = {"page": "pages", "database": "databases", "data_source": "data_sources", "block": "blocks"}[kind]
            obj = self.api.request("GET", f"/{endpoint}/{oid}")
        self.metadata[key] = obj
        if trashed(obj):
            self.exclude(oid)
            return True
        parent = obj.get("parent", {})
        for parent_kind in ("block", "page", "database", "data_source"):
            parent_id = parent.get(parent_kind + "_id")
            if parent_id and self.deleted(parent_kind, parent_id, refresh=refresh, active=active | {oid}):
                self.exclude(oid)
                return True
        return False

    def blocks_for(self, oid, *, kind="block"):
        try:
            blocks = list(self.api.pages("GET", f"/blocks/{oid}/children"))
        except NotionHTTPError as exc:
            # The page decorator validates page 404s. Nested block 404s need
            # fresh block metadata, including the ancestor chain, before skip.
            if kind == "page" or exc.status != 404 or not self.deleted(kind, oid, refresh=True):
                raise
            self.exclude(oid)
            return []
        return self.prepare_blocks(blocks)

    def prepare_blocks(self, blocks):
        blocks = [block for block in blocks if not trashed(block)]
        for block in blocks:
            kind = block.get("type")
            if kind == "child_page":
                self.page(block["id"])
            elif kind == "child_database":
                self.database(block["id"])
            elif kind == "synced_block":
                block["_children"] = self.synced_block_children(block)
            elif block.get("has_children"):
                block["_children"] = self.blocks_for(block["id"])
            if kind == "unsupported":
                self.warnings.append(f"Unsupported API block {block['id']}")
        return blocks

    def synced_block_children(self, block):
        """Read original synced-block children, including when this is a copy."""
        current = block
        origin_id = uid(block["id"])
        active = set()
        while True:
            current_id = uid(current["id"])
            if current_id in active:
                raise RuntimeError("Cyclic Notion synced block references")
            active.add(current_id)
            value = current.get("synced_block")
            if not isinstance(value, dict):
                raise RuntimeError(f"Missing synced block data for {current_id}")
            synced_from = value.get("synced_from")
            if synced_from is None:
                children = value.get("children")
                if children is None:
                    # Some API responses omit inline children; query the original.
                    children = self.blocks_for(current_id)
                return self.prepare_blocks(children)
            source_id = synced_from.get("block_id")
            if not source_id:
                raise RuntimeError(f"Missing synced block source for {current_id}")
            source_id = uid(source_id)
            if source_id in active:
                raise RuntimeError("Cyclic Notion synced block references")
            current = self.api.request("GET", f"/blocks/{source_id}")
            if self.deleted("block", source_id, metadata=current):
                self.exclude(origin_id)
                return []

    def full_properties(self, obj):
        for name, prop in list(obj.get("properties", {}).items()):
            kind = prop.get("type")
            if kind not in {"title", "rich_text", "relation", "people", "rollup"}:
                continue
            # Page arrays below the documented 25-item cap are complete.
            if kind != "rollup" and len(prop.get(kind, [])) < 25 and not prop.get("has_more"):
                continue
            # At the cap, fetch the property endpoint to avoid silent truncation.
            data = self.api.request("GET", f"/pages/{obj['id']}/properties/{quote(prop['id'], safe='%')}", params={"page_size": 100})
            if data.get("object") != "list":
                obj["properties"][name] = {**prop, **data}
                continue
            results = list(data.get("results", []))
            seen = set()
            while data.get("has_more"):
                cursor = data.get("next_cursor")
                if not cursor or cursor in seen:
                    raise RuntimeError("Invalid property pagination")
                seen.add(cursor)
                data = self.api.request("GET", f"/pages/{obj['id']}/properties/{quote(prop['id'], safe='%')}", params={"page_size": 100, "start_cursor": cursor})
                results.extend(data.get("results", []))
            if kind == "rollup":
                obj["properties"][name] = {**prop, "rollup": data.get("property_item", {}).get("rollup", prop.get("rollup")), "_items": results}
            else:
                obj["properties"][name] = {**prop, kind: [x[kind] for x in results if kind in x], "has_more": False}

    @live_resource("page")
    def page(self, oid, metadata=None):
        oid = uid(oid)
        if oid in self.objects:
            return
        obj = metadata if metadata is not None else self.api.request("GET", f"/pages/{oid}")
        if self.deleted("page", oid, metadata=obj):
            return
        self.objects[oid] = obj
        self.full_properties(obj)
        self.blocks[oid] = self.blocks_for(oid, kind="page")
        self.markdown[oid] = self.markdown_for(oid, self.blocks[oid])
        if "<unknown " in self.markdown[oid]:
            self.warnings.append(f"Markdown has unsupported blocks on {oid}; raw blocks retained")
        if len(self.markdown) % 50 == 0:
            log.info("Read %d pages (%d API requests)", len(self.markdown), self.api.calls)

    def markdown_for(self, oid, blocks):
        # The Markdown endpoint also accepts block IDs for unloaded subtrees.
        unsupported = set()
        def collect(items):
            for block in items:
                if block.get("type") == "unsupported":
                    unsupported.add(uid(block["id"]))
                collect(block.get("_children", []))
        collect(blocks)
        def incomplete():
            return RuntimeError(f"Incomplete Markdown for {oid}; current backup remains unchanged")
        def retrieve(target, active):
            target = uid(target)
            if target in active:
                raise incomplete()
            data = self.api.request("GET", f"/pages/{target}/markdown", params={"include_transcript": "true"})
            text = data["markdown"]
            unknown = [uid(item) for item in data.get("unknown_block_ids", [])]
            if data.get("truncated") and not unknown:
                raise incomplete()
            for block_id in unknown:
                tags = [match.group(0) for match in re.finditer(r"<unknown\b[^>]*?/>", text)
                        if block_id.replace("-", "") in match.group(0).replace("-", "")]
                if not tags:
                    raise incomplete()
                # A known unsupported block is retained in raw JSON and explicitly
                # reported; it cannot be recovered by requesting the same subtree.
                if block_id in unsupported:
                    continue
                replacement = retrieve(block_id, active | {target})
                for tag in tags:
                    text = text.replace(tag, replacement)
            return text
        return retrieve(oid, set())

    @live_resource("data_source")
    def source(self, oid):
        oid = uid(oid)
        if oid in self.objects:
            return
        obj = self.api.request("GET", f"/data_sources/{oid}")
        if self.deleted("data_source", oid, metadata=obj):
            return
        self.objects[oid] = obj
        self.rows[oid] = []
        for row in self.api.pages('POST', f'/data_sources/{oid}/query'):
            if trashed(row):
                self.exclude(uid(row['id']))
                continue
            self.rows[oid].append({'id': row['id']})
            self.page(row['id'], row)
        parent = obj.get("database_parent", {}).get("database_id") or obj.get("parent", {}).get("database_id")
        if parent:
            self.database(parent)

    @live_resource("database")
    def database(self, oid):
        oid = uid(oid)
        if oid in self.objects:
            return
        obj = self.api.request("GET", f"/databases/{oid}")
        if self.deleted("database", oid, metadata=obj):
            return
        self.objects[oid] = obj
        for source in obj.get("data_sources", []):
            self.source(source["id"])

    def run(self, roots=()):
        # Fail quickly on a wrong credential or lost access to required roots.
        for kind, oid in roots:
            endpoint = "databases" if kind == "database" else "pages"
            self.api.request("GET", f"/{endpoint}/{uid(oid)}")
        # Search supplements traversal; it alone is not an exhaustive inventory.
        for kind in ("page", "data_source"):
            for obj in self.api.pages("POST", "/search", filter={"value": kind, "property": "object"}):
                if trashed(obj):
                    self.exclude(uid(obj["id"]))
                    continue
                if kind == "page":
                    self.page(obj["id"], obj)
                else:
                    self.source(obj["id"])
        for kind, oid in roots:
            (self.database if kind == "database" else self.page)(oid)
        # Remove descendants already visited before their parent was trashed.
        for oid, obj in list(self.objects.items()):
            if self.deleted(obj["object"], oid, metadata=obj):
                self.exclude(oid)
        for oid, rows in self.rows.items():
            self.rows[oid] = [row for row in rows if uid(row["id"]) not in self.excluded]
        if not self.objects and not self.excluded:
            raise RuntimeError("No accessible content; nothing published")
        return self


def walk_files(value):
    if isinstance(value, Mapping):
        if value.get("type") == "file" and isinstance(value.get("file"), dict) and value["file"].get("url"):
            yield value["file"]["url"]
        for item in value.values():
            yield from walk_files(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_files(item)


def refreshed_file_url(crawl, url):
    """Re-fetch its owner if a signed URL expired during a long workspace crawl."""
    def owners(value):
        if isinstance(value, Mapping):
            if value.get("object") in {"page", "block", "database"} and value.get("id"):
                if url in set(walk_files(value)):
                    yield value
            for item in value.values():
                yield from owners(item)
        elif isinstance(value, list):
            for item in value:
                yield from owners(item)
    candidates = list(owners([crawl.objects, crawl.blocks]))
    candidates.sort(key=lambda obj: obj.get("object") != "block")
    for obj in candidates:
        endpoint = {"page": "pages", "block": "blocks", "database": "databases"}[obj["object"]]
        fresh = crawl.api.request("GET", f"/{endpoint}/{obj['id']}")
        if obj["object"] == "page":
            crawl.full_properties(fresh)
        for candidate in walk_files(fresh):
            if urlparse(candidate).path == urlparse(url).path:
                return candidate
    raise RuntimeError("Unable to refresh expired attachment URL")


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def download(url, max_bytes, destination):
    # No API bearer headers; bounded chunks go directly to staging on disk.
    parsed = urlparse(url)
    if parsed.scheme != 'https':
        raise RuntimeError('Notion attachment is not HTTPS')
    digest, total = hashlib.sha256(), 0
    try:
        with requests.get(url, stream=True, timeout=(10, 60)) as response:
            if not response.ok:
                raise RuntimeError(f'Attachment download HTTP {response.status_code}')
            with destination.open('wb') as stream:
                for chunk in response.iter_content(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError('Attachment exceeds NOTION_MAX_FILE_BYTES')
                    stream.write(chunk)
                    digest.update(chunk)
        return digest.hexdigest(), total
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def object_paths(objects):
    paths, active = {}, set()
    def resolve(oid):
        if oid in paths:
            return paths[oid]
        if oid in active:
            raise RuntimeError("Cycle in Notion parent hierarchy")
        active.add(oid)
        obj = objects[oid]
        safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title(obj)).strip(" .")[:70] or "Untitled"
        safe = safe.encode("utf-8")[:170].decode("utf-8", errors="ignore").rstrip(" .") or "Untitled"
        parent = obj.get("database_parent") or obj.get("parent", {})
        parent_id = parent.get(parent.get("type"))
        if isinstance(parent_id, str):
            parent_id = uid(parent_id) if re.fullmatch(r"[0-9a-fA-F-]{32,36}", parent_id) else parent_id
        base = resolve(parent_id) if parent_id in objects else Path("workspace")
        paths[oid] = base / (safe + " " + oid.replace("-", ""))
        active.remove(oid)
        return paths[oid]
    for oid in objects:
        resolve(oid)
    return paths


def source_links(text, base, paths, objects, aliases, destinations):
    """Resolve legacy export links; report source-authored orphan references."""
    missing = set()
    identifier = r"[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    def replace(ref):
        if re.match(r'^\[*[A-Za-z][A-Za-z0-9+.-]*:', ref) or ref.startswith(('#', '//')):
            return ref
        rooted = re.fullmatch(r'/(' + identifier + r')(?:[?#].*)?', ref, flags=re.I)
        legacy = re.search(r'(' + identifier + r')\.md$', unquote(ref), flags=re.I)
        target = None
        if rooted:
            target = uid(rooted.group(1))
            if target not in objects:
                return 'https://www.notion.so' + ref
        elif legacy:
            candidates = aliases.get(uid(legacy.group(1)), set())
            if len(candidates) == 1:
                target = next(iter(candidates))
        if target in objects:
            leaf = 'page.md' if objects[target].get('object') == 'page' else 'index.md'
            return quote(Path(os.path.relpath(paths[target] / leaf, base)).as_posix(), safe='/.')
        destination = (Path('/snapshot') / base / unquote(ref.split('#', 1)[0])).resolve()
        if destination not in destinations:
            missing.add(ref)
        return ref
    text = re.sub(r'(\[[^\]]*\]\()([^\)]+)(\))', lambda m: m[1] + replace(m[2]) + m[3], text)
    text = re.sub(r'((?:src|url|href)=")([^"]+)(")', lambda m: m[1] + replace(m[2]) + m[3], text)
    return text, sorted(missing)


def snapshot(root, crawl, *, retention=1, downloader=None, max_file_bytes=536870912):
    previous = root / "current"
    previous_manifest = json.loads((previous / "manifest.json").read_text()) if (previous / "manifest.json").is_file() else {}
    previous_files = previous_manifest.get("files", {})
    staging = new_staging(root)
    files, reused = {}, 0
    def write(path, content):
        nonlocal reused
        relative = str(path)
        digest = hashlib.sha256(content).hexdigest()
        destination = staging / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        old = previous / path
        if previous_files.get(relative, {}).get("sha256") == digest and old.is_file() and file_hash(old) == digest:
            try:
                os.link(old, destination)
            except OSError:
                shutil.copyfile(old, destination)
            reused += 1
        else:
            destination.write_bytes(content)
        files[relative] = {"sha256": digest, "bytes": len(content)}
    try:
        paths = object_paths(crawl.objects)
        assets = {}
        aliases = {}
        for oid, obj in crawl.objects.items():
            aliases.setdefault(oid, set()).add(oid)
            old_id = re.search(r'([0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$', title(obj), flags=re.I)
            if old_id:
                aliases.setdefault(uid(old_id[1]), set()).add(oid)
        unresolved_links = []
        urls = set(walk_files([crawl.objects, crawl.blocks]))
        # Native markdown can include files not represented in block JSON.
        for text in crawl.markdown.values():
            for url in re.findall(r'https://[^\s<>"\)]+', text):
                try:
                    host = urlparse(html.unescape(url)).hostname or ""
                except ValueError:
                    # Arbitrary page text can contain placeholder/example URLs.
                    # Explicit file objects remain subject to strict download checks.
                    continue
                if host.endswith((".notion-static.com", ".notion.so", ".amazonaws.com")) and ("X-Amz-" in url or "secure.notion" in host):
                    urls.add(html.unescape(url))
        log.info("Downloading %d attachment URLs", len(urls))
        temporary_asset = staging / '.attachment.tmp'
        def fetch_asset(url):
            if downloader is None:
                return download(url, max_file_bytes, temporary_asset)
            # Explicit injected byte downloaders support offline regression fixtures.
            content = downloader(url, max_file_bytes)
            temporary_asset.write_bytes(content)
            return hashlib.sha256(content).hexdigest(), len(content)
        for url in sorted(urls):
            try:
                digest, size = fetch_asset(url)
            except RuntimeError as exc:
                if "HTTP 403" not in str(exc):
                    raise
                fresh_url = refreshed_file_url(crawl, url)
                digest, size = fetch_asset(fresh_url)
            suffix = Path(urlparse(url).path).suffix
            if not re.fullmatch(r"\.[a-zA-Z0-9]{1,10}", suffix):
                suffix = ""
            path = Path('assets') / (digest + suffix)
            relative = str(path)
            if relative not in files:
                destination, old = staging / path, previous / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                if previous_files.get(relative, {}).get('sha256') == digest and old.is_file() and file_hash(old) == digest:
                    try:
                        os.link(old, destination)
                    except OSError:
                        shutil.copyfile(old, destination)
                    temporary_asset.unlink()
                    reused += 1
                else:
                    temporary_asset.replace(destination)
                files[relative] = {'sha256': digest, 'bytes': size}
            else:
                temporary_asset.unlink()
            assets[url] = path
        destinations = {(Path('/snapshot') / paths[oid] / ('page.md' if obj.get('object') == 'page' else 'index.md')).resolve()
                        for oid, obj in crawl.objects.items()}
        destinations.update((Path('/snapshot') / path).resolve() for path in assets.values())
        log.info("Writing %d objects to the staged snapshot", len(crawl.objects))
        for oid, obj in sorted(crawl.objects.items()):
            base = paths[oid]
            write(Path("json") / oid / "content.json", json_bytes(obj))
            if oid in crawl.blocks:
                write(Path("json") / oid / "blocks.json", json_bytes(crawl.blocks[oid]))
            if oid in crawl.markdown:
                text = crawl.markdown[oid]
                write(Path('json') / oid / 'markdown.json', json_bytes({'markdown': text}))
                for url, path in assets.items():
                    local = quote(Path(os.path.relpath(path, base)).as_posix(), safe="/.")
                    text = text.replace(url, local).replace(html.escape(url, quote=True), local)
                # Resolve local links in one pass, including database references.
                pattern = r'https://(?:(?:www|app)\.)?notion\.(?:so|com)/[^\s<>"\)]*?([0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?:\?[^\s<>"\)]*)?'
                def local_reference(match):
                    target = uid(match.group(1))
                    if target not in paths:
                        return match.group(0)
                    leaf = "page.md" if crawl.objects[target].get("object") == "page" else "index.md"
                    return quote(Path(os.path.relpath(paths[target] / leaf, base)).as_posix(), safe="/.")
                text = re.sub(pattern, local_reference, text, flags=re.I)
                text, missing_links = source_links(text, base, paths, crawl.objects, aliases, destinations)
                for ref in missing_links:
                    unresolved_links.append({'page_id': oid, 'url': ref})
                if missing_links:
                    crawl.warnings.append(f"{len(missing_links)} unresolved original relative links on {oid}; source Markdown retained")
                write(base / "page.md", text.encode())
            else:
                entries = [f"# {title(obj)}\n"]
                if oid in crawl.rows:
                    entries.append("[Open database as CSV](rows.csv)\n")
                for child, child_obj in sorted(crawl.objects.items()):
                    par = child_obj.get("parent", {})
                    if par.get(par.get("type")) == oid:
                        leaf = "page.md" if child_obj.get("object") == "page" else "index.md"
                        entries.append(f"- [{title(child_obj)}]({quote(Path(os.path.relpath(paths[child] / leaf, base)).as_posix(), safe='/')})")
                write(base / "index.md", ("\n".join(entries) + "\n").encode())
            if oid in crawl.rows:
                rows = [crawl.objects[uid(row["id"])] for row in crawl.rows[oid]]
                keys = sorted(set(obj.get("properties", {})) | {key for row in rows for key in row.get("properties", {})})
                out = io.StringIO(newline="")
                writer = csv.writer(out)
                writer.writerow(["_notion_page_id", "_local_page", *keys])
                for row in rows:
                    values = []
                    for key in keys:
                        value = cell(row.get("properties", {}).get(key, {}))
                        for url, asset_path in assets.items():
                            value = value.replace(url, Path(os.path.relpath(asset_path, base)).as_posix())
                        values.append(value)
                    writer.writerow([row["id"], os.path.relpath(paths[uid(row["id"])] / "page.md", base), *values])
                write(base / "rows.csv", out.getvalue().encode())
                write(Path("json") / oid / "rows.json", json_bytes(rows))
        def navigation(oid):
            label = title(crawl.objects[oid]).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("\n", " ")
            leaf = 'page.md' if oid in crawl.markdown else 'index.md'
            return f"[{label}]({quote((paths[oid] / leaf).as_posix(), safe='/')})"
        ordered = sorted(paths, key=lambda oid: (title(crawl.objects[oid]).casefold(), paths[oid].as_posix()))
        roots = [oid for oid in ordered if len(paths[oid].parts) == 2]
        directory = ["# Notion workspace backup", "", "Open the links below in a Markdown viewer. Database CSV files open in a spreadsheet application.", "", "## Main pages and databases", ""]
        directory.extend(f"- {navigation(oid)}" for oid in roots)
        directory.extend(["", "## All pages and databases (alphabetical)", ""])
        for oid in ordered:
            context = ' / '.join(part.rsplit(' ', 1)[0] for part in paths[oid].parts[1:-1])
            context = context.replace("`", "'").replace("\n", " ")
            directory.append(f"- {navigation(oid)}" + (f" — `{context}`" if context else ""))
        write(Path("index.md"), ("\n".join(directory) + "\n").encode())
        previous_ids = set(previous_manifest.get("objects", {}))
        current_ids = set(crawl.objects)
        missing = sorted(previous_ids - current_ids)
        unexplained = []
        previous_objects = previous_manifest.get("objects", {})
        for oid in sorted(missing, key=lambda item: len(Path(previous_objects[item]["path"]).parts)):
            old_path = Path(previous_objects[oid]["path"])
            if any(parent in crawl.excluded and old_path.is_relative_to(Path(info["path"]))
                   for parent, info in previous_objects.items()):
                crawl.exclude(oid)
            if oid in crawl.excluded:
                continue
            kind = previous_manifest["objects"][oid].get("type")
            try:
                deleted = crawl.api is not None and crawl.deleted(kind, oid, refresh=True)
            except NotionHTTPError as exc:
                if exc.status != 404:
                    raise
                deleted = False
            if not deleted:
                unexplained.append(oid)
        if unexplained and os.environ.get("NOTION_ALLOW_MISSING", "0") != "1":
            raise RuntimeError(f"{len(unexplained)} previously backed-up objects are missing without confirmed deletion (possibly lost permissions); set NOTION_ALLOW_MISSING=1 only after review")
        name = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S%fZ")
        manifest = {"timestamp": datetime.now(timezone.utc).isoformat(), "api_version": VERSION,
                    "status": "complete_with_warnings" if crawl.warnings else "complete",
                    "scope": "API-accessible content; search plus recursive traversal, not a native workspace export",
                    "objects": {oid: {"type": obj.get("object"), "path": str(paths[oid])} for oid, obj in crawl.objects.items()},
                    "files": files, "warnings": crawl.warnings, "unresolved_source_links": unresolved_links,
                    "missing_since_previous": missing, "confirmed_deleted": sorted(crawl.excluded), "reused_files": reused,
                    "external_media": "External embeds/links remain external; Notion-hosted file objects are downloaded"}
        (staging / "manifest.json").write_bytes(json_bytes(manifest))
        (staging / "COMPLETE").write_text(name + "\n")
        (root / "snapshots").mkdir(exist_ok=True)
        published = root / "snapshots" / name
        staging.rename(published)
        point_current(root, published)
        if retention > 0:
            prune_snapshots(root, retention)
        return manifest
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def record_run(root, status):
    temporary = root / ".last-run.tmp"
    temporary.write_bytes(json_bytes({"status": status, "timestamp": datetime.now(timezone.utc).isoformat()}))
    os.replace(temporary, root / "last-run.json")


def health(root, max_age):
    try:
        current = root / "current"
        manifest = json.loads((current / "manifest.json").read_text())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(manifest["timestamp"])).total_seconds()
        last_run = root / "last-run.json"
        if last_run.is_file() and json.loads(last_run.read_text()).get("status") == "failed":
            return False
        return (current / "COMPLETE").is_file() and manifest["status"] in {"complete", "complete_with_warnings"} and 0 <= age <= max_age
    except (OSError, ValueError, KeyError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, default=Path(os.environ.get("NOTION_BACKUP_DIR", "/backup")))
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--check-access", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("NOTION_BACKUP_INTERVAL_SECONDS", "14400")))
    args = parser.parse_args(argv)
    if args.health:
        ok = health(args.backup_dir, int(os.environ.get("NOTION_BACKUP_MAX_AGE", "43200")))
        print("healthy" if ok else "no recent completed backup")
        return 0 if ok else 1
    if args.interval <= 0:
        parser.error("--interval must be positive")
    api = API(os.environ.get("NOTION_PAT") or os.environ.get("NOTION_TOKEN"))
    roots = []
    for raw in os.environ.get("NOTION_ROOTS", "").split(","):
        if raw.strip():
            kind, oid = raw.strip().split(":", 1)
            if kind not in {"page", "database"}:
                raise ValueError("NOTION_ROOTS entries must be page:ID or database:ID")
            roots.append((kind, uid(oid)))
    if args.check_access:
        identity = api.request("GET", "/users/me")
        print("API credential valid; identity type:", identity.get("type", "user"))
        for kind, oid in roots:
            api.request("GET", f"/{'pages' if kind == 'page' else 'databases'}/{oid}")
            print("Root accessible:", kind, oid)
        return 0
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    with (args.backup_dir / ".backup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Notion backup is running") from None
        while True:
            started = time.monotonic()
            api.calls = 0
            try:
                log.info('Starting workspace export (interval %ds; API delay %.2fs)', args.interval, api.delay)
                with tempfile.TemporaryDirectory(prefix='.notion-crawl-', dir=args.backup_dir) as spool:
                    result = snapshot(args.backup_dir, Crawl(api, spool_dir=spool).run(roots),
                                      retention=int(os.environ.get('NOTION_BACKUP_RETENTION', '1')),
                                      max_file_bytes=int(os.environ.get('NOTION_MAX_FILE_BYTES', '536870912')))
                record_run(args.backup_dir, "complete")
                log.info("Published %d objects, %d files, %d reused, %d warnings", len(result["objects"]), len(result["files"]), result["reused_files"], len(result["warnings"]))
            except Exception as exc:
                record_run(args.backup_dir, "failed")
                log.error("Backup failed: %s", str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
                if not args.watch:
                    return 1
            if not args.watch:
                return 0
            remaining = max(1, args.interval - (time.monotonic() - started))
            log.info('Next workspace export in %.0f seconds', remaining)
            time.sleep(remaining)


if __name__ == "__main__":
    handlers = [logging.StreamHandler()]
    if os.environ.get("NOTION_LOG_DIR"):
        from logging.handlers import RotatingFileHandler
        folder = Path(os.environ["NOTION_LOG_DIR"])
        folder.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(folder / "notion-backup.log", maxBytes=5000000, backupCount=2))
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        log.error("%s", exc)
        raise SystemExit(1)
