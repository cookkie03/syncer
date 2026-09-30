#!/usr/bin/env python3
"""Incremental Google Contacts backup service."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

for shared_path in ["/app/project-settings", str(Path(__file__).resolve().parent.parent / "settings")]:
    if shared_path not in sys.path:
        sys.path.insert(0, shared_path)
from config_loader import cfg  # noqa: E402
from google_auth import load_google_client, token_matches_client  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("google-contacts-backup")

GOOGLE_CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts"
GOOGLE_CONTACTS_READONLY_SCOPE = "https://www.googleapis.com/auth/contacts.readonly"

BACKUP_DIR = Path(cfg("google_contacts_backup.backup_dir", os.environ.get("BACKUP_DIR", "/backup")))
BACKUP_INTERVAL_MINUTES = cfg("google_contacts_backup.backup_interval_minutes", 1440, int)
GOOGLE_API_DELAY = cfg("google_contacts_backup.google_api_delay", 0.5, float)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def load_google_token_payload(token_file: str | Path) -> dict:
    return json.loads(Path(token_file).read_text(encoding="utf-8"))


def google_contacts_scopes_from_token_payload(token_payload: dict) -> list[str]:
    raw_scope = token_payload.get("scope") or token_payload.get("scopes")

    if isinstance(raw_scope, str):
        scopes = [scope for scope in raw_scope.split() if scope]
    elif isinstance(raw_scope, list):
        scopes = [str(scope).strip() for scope in raw_scope if str(scope).strip()]
    else:
        scopes = []

    if scopes:
        return scopes
    return [GOOGLE_CONTACTS_SCOPE]


def sanitize_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return safe or "unnamed"


def normalize_vcard(vcard_text: str) -> str:
    text = vcard_text.strip()
    return text + "\n"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def atomic_write_json(path: Path, payload: dict) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def update_latest_pointer(backup_root: Path, snapshot_name: str, snapshot_dir: Path) -> None:
    latest_link = backup_root / "latest"
    link_target = snapshot_dir.relative_to(backup_root)
    if latest_link.is_dir() and not latest_link.is_symlink():
        archive = backup_root / ("legacy-latest-" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f"))
        latest_link.rename(archive)
    elif latest_link.is_symlink() or latest_link.exists():
        latest_link.unlink()
    latest_link.symlink_to(link_target)

    atomic_write_json(
        backup_root / "latest.json",
        {
            "snapshot": snapshot_name,
            "snapshot_dir": str(snapshot_dir),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def load_previous_manifest(backup_root: Path) -> tuple[str | None, dict]:
    latest_json = backup_root / "latest.json"
    if not latest_json.exists():
        return None, {}

    latest = json.loads(latest_json.read_text(encoding="utf-8"))
    snapshot_name = latest.get("snapshot")
    if not snapshot_name:
        return None, {}

    manifest_path = backup_root / "snapshots" / snapshot_name / "manifest.json"
    if not manifest_path.exists():
        return None, {}

    return snapshot_name, json.loads(manifest_path.read_text(encoding="utf-8"))


def write_incremental_snapshot(backup_root: Path, contacts: dict[str, str], timestamp: str | None = None) -> dict:
    backup_root.mkdir(parents=True, exist_ok=True)
    previous_snapshot, previous_manifest = load_previous_manifest(backup_root)
    previous_contacts = previous_manifest.get("contacts", {})
    if previous_contacts and not contacts:
        raise RuntimeError("Google returned zero contacts; previous backup remains current")

    snapshots_root = backup_root / "snapshots"
    snapshots_root.mkdir(parents=True, exist_ok=True)

    snapshot_name = timestamp or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snapshot_dir = snapshots_root / snapshot_name
    contacts_dir = snapshot_dir / "contacts"
    contacts_dir.mkdir(parents=True, exist_ok=True)

    new_count = 0
    changed_count = 0
    unchanged_count = 0
    manifest_contacts: dict[str, dict] = {}

    for uid in sorted(contacts):
        vcard_text = normalize_vcard(contacts[uid])
        digest = sha256_text(vcard_text)
        filename = f"{sanitize_filename(uid)}.vcf"
        destination = contacts_dir / filename

        previous_entry = previous_contacts.get(uid)
        if previous_entry and previous_entry.get("sha256") == digest and previous_snapshot:
            previous_file = backup_root / "snapshots" / previous_snapshot / "contacts" / previous_entry["file"]
            if previous_file.exists():
                os.link(previous_file, destination)
                unchanged_count += 1
            else:
                atomic_write_text(destination, vcard_text)
                changed_count += 1
        else:
            atomic_write_text(destination, vcard_text)
            if previous_entry:
                changed_count += 1
            else:
                new_count += 1

        manifest_contacts[uid] = {
            "file": filename,
            "sha256": digest,
        }

    previous_uids = set(previous_contacts)
    current_uids = set(contacts)
    deleted_uids = sorted(previous_uids - current_uids)

    combined_vcf = "".join(normalize_vcard(contacts[uid]) for uid in sorted(contacts))
    atomic_write_text(snapshot_dir / "all_contacts.vcf", combined_vcf)

    manifest = {
        "timestamp": snapshot_name,
        "snapshot_dir": str(snapshot_dir),
        "previous_snapshot": previous_snapshot,
        "contacts": manifest_contacts,
        "deleted_uids": deleted_uids,
        "stats": {
            "total": len(contacts),
            "new": new_count,
            "changed": changed_count,
            "unchanged": unchanged_count,
            "deleted": len(deleted_uids),
        },
    }
    atomic_write_json(snapshot_dir / "manifest.json", manifest)
    update_latest_pointer(backup_root, snapshot_name, snapshot_dir)
    return manifest


def google_to_vcard(person: dict, uid: str) -> str:
    import vobject

    vcard = vobject.vCard()
    vcard.add("uid").value = uid

    names_list = person.get("names", [])
    names = names_list[0] if names_list else {}
    display_name = names.get("displayName", "").strip() or "Senza Nome"
    vcard.add("fn").value = display_name
    vcard.add("n").value = vobject.vcard.Name(
        family=names.get("familyName", ""),
        given=names.get("givenName", ""),
        additional=names.get("middleName", ""),
        prefix=names.get("honorificPrefix", ""),
        suffix=names.get("honorificSuffix", ""),
    )

    for email in person.get("emailAddresses", []):
        item = vcard.add("email")
        item.value = email["value"]
        item.params["TYPE"] = [email.get("type", "home").upper()]

    for phone in person.get("phoneNumbers", []):
        item = vcard.add("tel")
        item.value = phone["value"]
        item.params["TYPE"] = [phone.get("type", "mobile").upper()]

    if person.get("birthdays"):
        date = person["birthdays"][0].get("date", {})
        month = date.get("month")
        day = date.get("day")
        year = date.get("year")
        if month and day:
            if year:
                vcard.add("bday").value = f"{year:04d}-{month:02d}-{day:02d}"
            else:
                vcard.add("bday").value = f"--{month:02d}-{day:02d}"

    biographies = [entry["value"] for entry in person.get("biographies", []) if entry.get("value")]
    if biographies:
        vcard.add("note").value = "\n".join(biographies)

    for address in person.get("addresses", []):
        item = vcard.add("adr")
        item.value = vobject.vcard.Address(
            street=address.get("streetAddress", ""),
            city=address.get("city", ""),
            region=address.get("region", ""),
            code=address.get("postalCode", ""),
            country=address.get("country", ""),
            extended=address.get("extendedAddress", ""),
            box=address.get("poBox", ""),
        )
        item.params["TYPE"] = [address.get("type", "home").upper()]

    organizations = person.get("organizations", [])
    if organizations:
        organization = organizations[0]
        if organization.get("name"):
            vcard.add("org").value = [organization["name"]]
        if organization.get("title"):
            vcard.add("title").value = organization["title"]

    for url in person.get("urls", []):
        if url.get("value"):
            vcard.add("url").value = url["value"]

    nicknames = person.get("nicknames", [])
    if nicknames and nicknames[0].get("value"):
        vcard.add("nickname").value = nicknames[0]["value"]

    return vcard.serialize()


class GoogleContactsClient:
    PERSON_FIELDS = (
        "names,emailAddresses,phoneNumbers,birthdays,externalIds,biographies,"
        "metadata,addresses,organizations,urls,nicknames"
    )

    def __init__(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_file = require_env("GOOGLE_CONTACTS_TOKEN_FILE")
        token_payload = load_google_token_payload(token_file)
        client_path = Path(require_env("GOOGLE_CLIENT_JSON_FILE"))
        client = load_google_client(client_path)
        if not token_matches_client(token_payload, client, ("token", "refresh_token"), GOOGLE_CONTACTS_SCOPE):
            raise RuntimeError("Google Contacts token does not match setup/google/client_secret.json")
        scopes = google_contacts_scopes_from_token_payload(token_payload)
        credentials = Credentials.from_authorized_user_info({**token_payload, **client}, scopes)
        if not credentials.valid and credentials.refresh_token:
            credentials.refresh(Request())
        log.info("Using Google Contacts token scopes: %s", " ".join(scopes))
        self.service = build("people", "v1", credentials=credentials)

    def fetch_all_contacts(self) -> dict[str, dict]:
        contacts: dict[str, dict] = {}
        page_token = None
        while True:
            response = (
                self.service.people()
                .connections()
                .list(
                    resourceName="people/me",
                    pageSize=1000,
                    pageToken=page_token,
                    personFields=self.PERSON_FIELDS,
                )
                .execute()
            )
            for person in response.get("connections", []):
                uid = None
                for external_id in person.get("externalIds", []):
                    if external_id.get("type") == "vCard-UID":
                        uid = external_id.get("value")
                        break
                if not uid:
                    uid = person["resourceName"].replace("/", "_")
                contacts[uid] = person
            page_token = response.get("nextPageToken")
            if not page_token:
                break
            time.sleep(GOOGLE_API_DELAY)
        return contacts


def run_backup() -> dict:
    client = GoogleContactsClient()
    contacts = client.fetch_all_contacts()
    vcf_contacts = {uid: google_to_vcard(person, uid) for uid, person in contacts.items()}
    manifest = write_incremental_snapshot(BACKUP_DIR, vcf_contacts)

    log.info("Backup complete")
    log.info("  Contacts: %d", manifest["stats"]["total"])
    log.info(
        "  New: %d, changed: %d, unchanged: %d, deleted: %d",
        manifest["stats"]["new"],
        manifest["stats"]["changed"],
        manifest["stats"]["unchanged"],
        manifest["stats"]["deleted"],
    )
    log.info("  Snapshot: %s", manifest["snapshot_dir"])
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Incremental Google Contacts backup")
    parser.add_argument("--watch", action="store_true", help="Run backups continuously")
    parser.add_argument(
        "--interval",
        type=int,
        default=BACKUP_INTERVAL_MINUTES * 60,
        help="Backup interval in seconds for watch mode",
    )
    args = parser.parse_args()

    if args.watch:
        log.info("Starting watch mode (backup every %d seconds)", args.interval)
        while True:
            try:
                run_backup()
            except Exception as exc:
                log.error("Backup failed: %s", exc)
            log.info("Waiting %d seconds...", args.interval)
            time.sleep(args.interval)
    else:
        run_backup()


if __name__ == "__main__":
    main()
