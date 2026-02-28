#!/usr/bin/env python3
"""
cleanup_synology — Deduplicate CardDAV contacts by Name and Email.
"""
import os
import sys
import logging
import vobject
import requests
from pathlib import Path
from sync import SimpleCardDAVClient, get_contact_fingerprint, load_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cleanup")

def main():
    # Load .env variables
    root = Path(__file__).parent.parent
    env_file = root / ".env"
    
    # Simple manual env loader
    if env_file.exists():
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

    CARDDAV_URL = os.environ.get("CARDDAV_URL")
    USER = os.environ.get("CALDAV_USERNAME")
    PASS = os.environ.get("CALDAV_PASSWORD")

    if not all([CARDDAV_URL, USER, PASS]):
        print("Error: Missing CardDAV credentials in .env")
        sys.exit(1)

    client = SimpleCardDAVClient(CARDDAV_URL, USER, PASS)
    log.info("Fetching contacts for deduplication...")
    contacts = client.get_all_contacts()
    log.info("Found %d total items on CardDAV", len(contacts))

    seen_fingerprints = {}
    duplicates_to_delete = []

    for c in contacts:
        fingerprint = get_contact_fingerprint(c["vcard"])
        if not fingerprint:
            log.warning("Skipping contact with no fingerprint (empty name/email): %s", c["url"])
            continue
        
        if fingerprint in seen_fingerprints:
            duplicates_to_delete.append(c)
        else:
            seen_fingerprints[fingerprint] = c

    log.info("Found %d unique contacts and %d duplicates.", len(seen_fingerprints), len(duplicates_to_delete))

    if not duplicates_to_delete:
        log.info("No duplicates found. Nothing to do.")
        return

    confirm = input(f"Proceed to DELETE {len(duplicates_to_delete)} duplicates from Synology? (y/N): ")
    if confirm.lower() != 'y':
        log.info("Abort.")
        return

    for idx, c in enumerate(duplicates_to_delete):
        try:
            log.info("[%d/%d] Deleting duplicate: %s", idx+1, len(duplicates_to_delete), c["url"])
            res = client.session.delete(c["url"])
            res.raise_for_status()
        except Exception as e:
            log.error("Failed to delete %s: %s", c["url"], e)

    log.info("Cleanup complete.")

if __name__ == "__main__":
    main()
