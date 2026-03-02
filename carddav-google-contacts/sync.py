#!/usr/bin/env python3
"""
carddav-google-contacts — Bidirectional CardDAV ↔ Google Contacts sync.

Fixes vs. previous version:
  - Proper addressbook discovery (no per-contact addressbooks)
  - Fingerprint-based initial matching (no duplicates on first sync)
  - UID anchoring via Google externalIds
  - Correct Google People API etag handling
"""
import os
import sys
import logging
import sqlite3
import uuid
import json
import re
import time
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
import vobject
import xml.etree.ElementTree as ET
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── CONFIGURATION ─────────────────────────────────────────────────────────
DB_FILE = os.environ.get("DB_FILE", "/data/sync_contacts.db")
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/data/backup"))
GOOGLE_TOKEN = os.environ.get("GOOGLE_CONTACTS_TOKEN_FILE")
CARDDAV_URL = os.environ.get("CARDDAV_URL", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

BACKUP_INTERVAL_MINUTES = int(os.environ.get("BACKUP_INTERVAL_MINUTES", "1440"))
LAST_BACKUP_FILE = BACKUP_DIR / ".last_backup"

SAFETY_DELETE_PCT = 0.20
SAFETY_MIN_STATE = 50

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sync")

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:carddav"}

# ── TYPE MAPPING (vCard ↔ Google People API) ─────────────────────────────
VCARD_TO_GOOGLE_TYPE = {
    "HOME": "home", "WORK": "work", "CELL": "mobile", "MOBILE": "mobile",
    "MAIN": "main", "FAX": "homeFax", "PAGER": "pager", "OTHER": "other",
    "VOICE": "home", "INTERNET": "home",
}
GOOGLE_TO_VCARD_TYPE = {v: k for k, v in VCARD_TO_GOOGLE_TYPE.items()}
GOOGLE_TO_VCARD_TYPE["mobile"] = "CELL"  # prefer CELL over MOBILE for vCard
GOOGLE_TO_VCARD_TYPE["homeFax"] = "FAX"


# ── FINGERPRINTING ────────────────────────────────────────────────────────
def normalize_phone(phone: str) -> str:
    """Strip all non-digit/+ chars for comparison."""
    return re.sub(r"[^0-9+]", "", phone)


def fingerprint_from_vcard(vcard_str: str) -> str:
    """Build a deterministic fingerprint from a vCard string."""
    try:
        v = vobject.readOne(vcard_str)
        fn = getattr(v, "fn", None)
        name = fn.value.strip().lower() if fn else ""
        emails = sorted(e.value.strip().lower() for e in getattr(v, "email_list", []))
        phones = sorted(normalize_phone(p.value) for p in getattr(v, "tel_list", []))
        email = emails[0] if emails else ""
        phone = phones[0] if phones else ""
        if not name and not email and not phone:
            return ""
        return f"{name}|{email}|{phone}"
    except Exception:
        return ""


def fingerprint_from_google(person: dict) -> str:
    """Build the same fingerprint from a Google People API person."""
    names = person.get("names", [{}])
    name = names[0].get("displayName", "").strip().lower() if names else ""
    emails = sorted(e["value"].strip().lower() for e in person.get("emailAddresses", []))
    phones = sorted(normalize_phone(p["value"]) for p in person.get("phoneNumbers", []))
    email = emails[0] if emails else ""
    phone = phones[0] if phones else ""
    if not name and not email and not phone:
        return ""
    return f"{name}|{email}|{phone}"


# ── DATA NORMALIZATION ────────────────────────────────────────────────────
def parse_date(d_str):
    if not d_str:
        return None
    # Truncate at T to handle vCard 4.0 datetime (e.g. "1990-01-15T00:00:00")
    d_str = d_str.split("T")[0].strip()
    clean = re.sub(r"[^0-9-]", "", d_str)
    try:
        if clean.startswith("--"):
            m = re.search(r"--(\d{2})-?(\d{2})", clean)
            if m:
                month, day = int(m.group(1)), int(m.group(2))
                if 1 <= month <= 12 and 1 <= day <= 31:
                    return {"month": month, "day": day}
            return None
        if "-" in clean:
            parts = clean.split("-")
            if len(parts) >= 3:
                year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                if 1 <= month <= 12 and 1 <= day <= 31:
                    return {"year": year, "month": month, "day": day}
        if len(clean) == 8:
            year, month, day = int(clean[:4]), int(clean[4:6]), int(clean[6:8])
            if 1 <= month <= 12 and 1 <= day <= 31:
                return {"year": year, "month": month, "day": day}
    except Exception:
        pass
    return None


def google_to_vcard(person: dict, uid: str) -> str:
    v = vobject.vCard()
    v.add("uid").value = uid

    names_list = person.get("names", [])
    names = names_list[0] if names_list else {}
    display_name = names.get("displayName", "").strip() or "Senza Nome"
    v.add("fn").value = display_name
    v.add("n").value = vobject.vcard.Name(
        family=names.get("familyName", ""),
        given=names.get("givenName", ""),
        additional=names.get("middleName", ""),
        prefix=names.get("honorificPrefix", ""),
        suffix=names.get("honorificSuffix", ""),
    )

    for e in person.get("emailAddresses", []):
        item = v.add("email")
        item.value = e["value"]
        g_type = e.get("type", "home")
        item.params["TYPE"] = [GOOGLE_TO_VCARD_TYPE.get(g_type, g_type.upper())]

    for ph in person.get("phoneNumbers", []):
        item = v.add("tel")
        item.value = ph["value"]
        g_type = ph.get("type", "mobile")
        item.params["TYPE"] = [GOOGLE_TO_VCARD_TYPE.get(g_type, g_type.upper())]

    if person.get("birthdays"):
        d = person["birthdays"][0].get("date")
        if d:
            month = d.get("month", 0)
            day = d.get("day", 0)
            if 1 <= month <= 12 and 1 <= day <= 31:
                yr = d.get("year") or 0
                if yr:
                    v.add("bday").value = f"{yr:04d}-{month:02d}-{day:02d}"
                else:
                    v.add("bday").value = f"--{month:02d}-{day:02d}"

    note_parts = [bio["value"] for bio in person.get("biographies", []) if bio.get("value")]
    if note_parts:
        v.add("note").value = "\n".join(note_parts)

    for addr in person.get("addresses", []):
        item = v.add("adr")
        item.value = vobject.vcard.Address(
            street=addr.get("streetAddress", ""),
            city=addr.get("city", ""),
            region=addr.get("region", ""),
            code=addr.get("postalCode", ""),
            country=addr.get("country", ""),
            extended=addr.get("extendedAddress", ""),
            box=addr.get("poBox", ""),
        )
        g_type = addr.get("type", "home")
        item.params["TYPE"] = [GOOGLE_TO_VCARD_TYPE.get(g_type, g_type.upper())]

    orgs = person.get("organizations", [])
    if orgs:
        org = orgs[0]
        if org.get("name"):
            v.add("org").value = [org["name"]]
        if org.get("title"):
            v.add("title").value = org["title"]

    for url in person.get("urls", []):
        if url.get("value"):
            v.add("url").value = url["value"]

    nicks = person.get("nicknames", [])
    if nicks and nicks[0].get("value"):
        v.add("nickname").value = nicks[0]["value"]

    return v.serialize()


def vcard_to_google(vcard_str: str, uid: str) -> dict:
    v = vobject.readOne(vcard_str)

    fn = getattr(v, "fn", None)
    display_name = fn.value.strip() if fn else "Senza Nome"
    n = getattr(v, "n", None)

    name_entry = {
        "displayName": display_name,
        "familyName": n.value.family if n else "",
        "givenName": n.value.given if n else "",
    }
    if n:
        if getattr(n.value, "additional", ""):
            name_entry["middleName"] = n.value.additional
        if getattr(n.value, "prefix", ""):
            name_entry["honorificPrefix"] = n.value.prefix
        if getattr(n.value, "suffix", ""):
            name_entry["honorificSuffix"] = n.value.suffix

    person = {
        "names": [name_entry],
        "externalIds": [{"value": uid, "type": "vCard-UID"}],
    }

    if hasattr(v, "email_list"):
        emails = []
        for e in v.email_list:
            types = e.params.get("TYPE", [])
            g_type = "other"
            for t in types:
                mapped = VCARD_TO_GOOGLE_TYPE.get(t.upper())
                if mapped:
                    g_type = mapped
                    break
            emails.append({"value": e.value, "type": g_type})
        person["emailAddresses"] = emails
    if hasattr(v, "tel_list"):
        phones = []
        for t in v.tel_list:
            types = t.params.get("TYPE", [])
            g_type = "other"
            for tp in types:
                mapped = VCARD_TO_GOOGLE_TYPE.get(tp.upper())
                if mapped:
                    g_type = mapped
                    break
            phones.append({"value": t.value, "type": g_type})
        person["phoneNumbers"] = phones
    if hasattr(v, "bday"):
        dt = parse_date(v.bday.value)
        if dt:
            person["birthdays"] = [{"date": dt}]
    if hasattr(v, "note_list"):
        parts = [n.value for n in v.note_list if n.value]
        if parts:
            person["biographies"] = [{"value": "\n".join(parts)}]
    elif hasattr(v, "note"):
        person["biographies"] = [{"value": v.note.value}]

    if hasattr(v, "adr_list"):
        addrs = []
        for a in v.adr_list:
            addr = {}
            val = a.value
            if val.street:
                addr["streetAddress"] = val.street
            if val.city:
                addr["city"] = val.city
            if val.region:
                addr["region"] = val.region
            if val.code:
                addr["postalCode"] = val.code
            if val.country:
                addr["country"] = val.country
            if val.extended:
                addr["extendedAddress"] = val.extended
            if val.box:
                addr["poBox"] = val.box
            if addr:
                types = a.params.get("TYPE", [])
                g_type = "home"
                for t in types:
                    mapped = VCARD_TO_GOOGLE_TYPE.get(t.upper())
                    if mapped:
                        g_type = mapped
                        break
                addr["type"] = g_type
                addrs.append(addr)
        if addrs:
            person["addresses"] = addrs

    org_val = getattr(v, "org", None)
    title_val = getattr(v, "title", None)
    if org_val or title_val:
        org_entry = {}
        if org_val and org_val.value:
            org_name = org_val.value[0] if isinstance(org_val.value, list) else org_val.value
            if org_name:
                org_entry["name"] = org_name
        if title_val and title_val.value:
            org_entry["title"] = title_val.value
        if org_entry:
            person["organizations"] = [org_entry]

    if hasattr(v, "url_list"):
        urls = [{"value": u.value} for u in v.url_list if u.value]
        if urls:
            person["urls"] = urls
    elif hasattr(v, "url") and v.url.value:
        person["urls"] = [{"value": v.url.value}]

    if hasattr(v, "nickname") and v.nickname.value:
        person["nicknames"] = [{"value": v.nickname.value}]

    return person


# ── CARDDAV CLIENT ────────────────────────────────────────────────────────
class CardDAVClient:
    def __init__(self):
        if not CARDDAV_URL:
            raise ValueError("CARDDAV_URL is missing in environment variables.")
        base = CARDDAV_URL if CARDDAV_URL.endswith("/") else CARDDAV_URL + "/"
        self.session = requests.Session()
        self.session.auth = (os.environ["CALDAV_USERNAME"], os.environ["CALDAV_PASSWORD"])
        self.addressbook_url = self._discover_addressbook(base)
        log.info(f"Using addressbook: {self.addressbook_url}")

    def _discover_addressbook(self, base_url: str) -> str:
        """PROPFIND to find the first real addressbook under base_url."""
        xml_body = (
            '<?xml version="1.0"?>'
            '<D:propfind xmlns:D="DAV:">'
            "<D:prop><D:resourcetype/></D:prop>"
            "</D:propfind>"
        )
        try:
            res = self.session.request(
                "PROPFIND",
                base_url,
                headers={"Depth": "1", "Content-Type": "application/xml"},
                data=xml_body,
            )
            res.raise_for_status()
            root = ET.fromstring(res.text)
            for resp in root.findall(".//d:response", NS):
                rt = resp.find(".//d:resourcetype", NS)
                if rt is not None and rt.find("c:addressbook", NS) is not None:
                    href = resp.find("d:href", NS).text
                    discovered = urljoin(base_url, href)
                    if not discovered.endswith("/"):
                        discovered += "/"
                    return discovered
        except Exception as e:
            log.warning(f"Addressbook discovery failed ({e}), falling back to base URL")

        # Fallback: assume base_url IS the addressbook
        return base_url

    def get_all_contacts(self) -> dict:
        """Returns {vcard_uid: {"href": str, "etag": str, "vcard": str}}."""
        xml_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<C:addressbook-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">'
            '<D:prop><D:getetag/><C:address-data/></D:prop>'
            '</C:addressbook-query>'
        )
        res = self.session.request(
            "REPORT",
            self.addressbook_url,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            data=xml_body,
        )
        res.raise_for_status()

        contacts = {}
        root = ET.fromstring(res.text)
        for response in root.findall(".//d:response", NS):
            href_node = response.find("d:href", NS)
            if href_node is None or not href_node.text:
                continue
            href = href_node.text
            # Skip the collection itself
            if href.endswith("/"):
                continue

            etag_node = response.find(".//d:propstat/d:prop/d:getetag", NS)
            vcard_node = response.find(".//d:propstat/d:prop/c:address-data", NS)
            if not (etag_node is not None and etag_node.text and vcard_node is not None and vcard_node.text):
                continue

            vcard_str = vcard_node.text.strip()
            if not vcard_str:
                continue

            try:
                parsed = vobject.readOne(vcard_str)
                uid = parsed.uid.value.strip()
            except Exception:
                continue
            full_url = urljoin(self.addressbook_url, href)
            contacts[uid] = {
                "href": full_url,
                "etag": etag_node.text.strip('"'),
                "vcard": vcard_str,
            }
        return contacts

    def put(self, url: str, data: str, etag: str | None = None) -> str:
        headers = {"Content-Type": "text/vcard; charset=utf-8"}
        if etag:
            headers["If-Match"] = f'"{etag}"'
        res = self.session.put(url, data=data.encode("utf-8"), headers=headers)
        res.raise_for_status()
        return res.headers.get("ETag", "").strip('"')

    def add(self, vcard_data: str) -> tuple[str, str]:
        """Create a new contact. Returns (url, etag)."""
        filename = f"{uuid.uuid4()}.vcf"
        target_url = urljoin(self.addressbook_url, filename)
        etag = self.put(target_url, vcard_data)
        return target_url, etag

    def delete(self, url: str):
        self.session.delete(url).raise_for_status()


# ── GOOGLE CLIENT ─────────────────────────────────────────────────────────
class GoogleClient:
    PERSON_FIELDS = "names,emailAddresses,phoneNumbers,birthdays,externalIds,biographies,metadata,addresses,organizations,urls,nicknames"
    UPDATE_FIELDS = "names,emailAddresses,phoneNumbers,externalIds,birthdays,biographies,addresses,organizations,urls,nicknames"

    def __init__(self):
        creds = Credentials.from_authorized_user_file(
            GOOGLE_TOKEN, ["https://www.googleapis.com/auth/contacts"]
        )
        if creds.expired:
            creds.refresh(Request())
        self.service = build("people", "v1", credentials=creds)

    def get_all_contacts(self) -> dict:
        """Returns {vcard_uid_or_resource: person_dict}.

        Contacts with externalId type=vCard-UID are keyed by that UID.
        Others are keyed by resourceName (people/XXXXX) — they need matching.
        """
        contacts = {}
        page_token = None
        while True:
            res = (
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
            for p in res.get("connections", []):
                ext_uid = None
                for ext in p.get("externalIds", []):
                    if ext.get("type") == "vCard-UID":
                        ext_uid = ext["value"]
                        break
                key = ext_uid if ext_uid else p["resourceName"]
                contacts[key] = p
            page_token = res.get("nextPageToken")
            if not page_token:
                break
        return contacts

    def create(self, body: dict) -> dict:
        res = self.service.people().createContact(body=body).execute()
        time.sleep(0.5)
        return res

    def update(self, resource_name: str, person_etag: str, body: dict) -> dict:
        body["etag"] = person_etag
        res = (
            self.service.people()
            .updateContact(
                resourceName=resource_name,
                updatePersonFields=self.UPDATE_FIELDS,
                body=body,
            )
            .execute()
        )
        time.sleep(0.5)
        return res

    def delete(self, resource_name: str):
        self.service.people().deleteContact(resourceName=resource_name).execute()
        time.sleep(0.5)


# ── STATE DATABASE ────────────────────────────────────────────────────────
def init_db(db_path: str) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """CREATE TABLE IF NOT EXISTS contacts (
            uid          TEXT PRIMARY KEY,
            google_res   TEXT,
            carddav_href TEXT,
            etag_google  TEXT,
            etag_carddav TEXT,
            fingerprint  TEXT
        )"""
    )
    # Migrate from old schema if needed
    _migrate_old_schema(db)
    db.commit()
    return db


def _migrate_old_schema(db: sqlite3.Connection):
    """If the old 'state' table exists, migrate rows to the new schema."""
    cur = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='state'")
    if not cur.fetchone():
        return
    log.info("Migrating old 'state' table to new 'contacts' schema...")
    rows = db.execute("SELECT uid, res_name, etag_c, etag_g FROM state").fetchall()
    for uid, res_name, etag_c, etag_g in rows:
        db.execute(
            "INSERT OR IGNORE INTO contacts (uid, google_res, etag_carddav, etag_google) VALUES (?,?,?,?)",
            (uid, res_name, etag_c, etag_g),
        )
    db.execute("DROP TABLE state")
    log.info(f"Migrated {len(rows)} rows from old schema.")


def load_state(db: sqlite3.Connection) -> dict:
    rows = db.execute("SELECT uid, google_res, carddav_href, etag_google, etag_carddav, fingerprint FROM contacts").fetchall()
    return {
        r[0]: {
            "google_res": r[1],
            "carddav_href": r[2],
            "etag_google": r[3],
            "etag_carddav": r[4],
            "fingerprint": r[5],
        }
        for r in rows
    }


# ── BACKUP ────────────────────────────────────────────────────────────────
def should_backup() -> bool:
    if not LAST_BACKUP_FILE.exists():
        return True
    try:
        last_ts = float(LAST_BACKUP_FILE.read_text().strip())
        return (time.time() - last_ts) >= (BACKUP_INTERVAL_MINUTES * 60)
    except Exception:
        return True


def mark_backup_done():
    LAST_BACKUP_FILE.write_text(str(time.time()))


def backup_to_vcf(contacts: dict, prefix: str):
    """Save all contacts to a single VCF file in BACKUP_DIR."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{prefix}_{ts}.vcf"
    path = BACKUP_DIR / filename
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for uid, data in contacts.items():
            # If it's from CardDAV it's already a dict with "vcard"
            # If it's from Google, we convert it to vCard string
            vcard = data["vcard"] if isinstance(data, dict) and "vcard" in data else google_to_vcard(data, uid)
            f.write(vcard.strip() + "\n")
    # Also save latest copy for ease of access
    latest = BACKUP_DIR / f"latest_{prefix}.vcf"
    shutil.copy2(path, latest)
    log.info(f"Backed up {len(contacts)} {prefix} contacts to {path}")


# ── SYNC ENGINE ───────────────────────────────────────────────────────────
def sync():
    dry = " [DRY RUN]" if DRY_RUN else ""
    log.info(f"Starting sync...{dry}")

    db = init_db(DB_FILE)
    state = load_state(db)

    # 1. Fetch all contacts from both sides
    google = GoogleClient()
    carddav = CardDAVClient()

    g_contacts = google.get_all_contacts()
    c_contacts = carddav.get_all_contacts()
    log.info(f"Fetched {len(g_contacts)} Google, {len(c_contacts)} CardDAV contacts")

    # Backup before making changes
    if should_backup():
        if c_contacts:
            backup_to_vcf(c_contacts, "carddav")
        if g_contacts:
            backup_to_vcf(g_contacts, "google")
        mark_backup_done()

    # 2. Build fingerprint indexes for unlinked contacts
    # Google contacts keyed by resourceName (no vCard-UID) need fingerprint matching
    g_by_fp = {}  # fingerprint -> (key, person)
    g_unlinked = {}  # resourceName -> person (no vCard-UID externalId)
    g_linked = {}  # vcard_uid -> person

    for key, person in g_contacts.items():
        if key.startswith("people/"):
            g_unlinked[key] = person
            fp = fingerprint_from_google(person)
            if fp:
                g_by_fp[fp] = (key, person)
        else:
            g_linked[key] = person

    c_by_fp = {}  # fingerprint -> (uid, data)
    c_unlinked_uids = set()  # UIDs not in state

    for uid, data in c_contacts.items():
        if uid not in state and uid not in g_linked:
            c_unlinked_uids.add(uid)
            fp = fingerprint_from_vcard(data["vcard"])
            if fp:
                c_by_fp[fp] = (uid, data)

    # 3. MATCH phase: pair unlinked contacts by fingerprint
    matched = {}  # vcard_uid -> (google_resource_name, person)
    for fp, (c_uid, c_data) in c_by_fp.items():
        if fp in g_by_fp:
            g_key, g_person = g_by_fp[fp]
            matched[c_uid] = (g_key, g_person)
            log.info(f"MATCH by fingerprint: '{fp[:40]}...' → CardDAV {c_uid[:20]}... ↔ Google {g_key}")

    # Apply matches: write externalId on Google + save to state
    for c_uid, (g_res, g_person) in matched.items():
        c_data = c_contacts[c_uid]
        try:
            if not DRY_RUN:
                # Anchor the UID on Google side
                body = vcard_to_google(c_data["vcard"], c_uid)
                body["etag"] = g_person["etag"]
                updated = google.update(g_res, g_person["etag"], body)
                new_g_etag = updated["etag"]
            else:
                new_g_etag = g_person.get("etag", "")

            if not DRY_RUN:
                db.execute(
                    "INSERT OR REPLACE INTO contacts (uid, google_res, carddav_href, etag_google, etag_carddav, fingerprint) VALUES (?,?,?,?,?,?)",
                    (c_uid, g_res, c_data["href"], new_g_etag, c_data["etag"], fingerprint_from_vcard(c_data["vcard"])),
                )
            # Remove from unlinked sets
            c_unlinked_uids.discard(c_uid)
            g_unlinked.pop(g_res, None)
            # Add to linked for the sync phase
            g_linked[c_uid] = g_person
        except Exception as e:
            log.error(f"Error anchoring match {c_uid}: {e}")

    db.commit()
    state = load_state(db)
    log.info(f"After matching: {len(matched)} pairs linked, {len(c_unlinked_uids)} CardDAV unlinked, {len(g_unlinked)} Google unlinked")

    # 4. Prepare the unified UID set for sync
    all_uids = set(state.keys()) | set(g_linked.keys()) | set(c_contacts.keys())
    # Also include unlinked Google contacts (they'll be created on CardDAV)
    # Map them temporarily by resourceName
    g_res_to_person = {p["resourceName"]: p for p in g_unlinked.values()}

    # ── SAFETY CHECKS ──────────────────────────────────────────────────
    # 1. Per-side emptiness check: if state knows N contacts on a side
    #    but that side now returns drastically fewer, something is wrong
    #    (e.g. API failure, auth issue, server bug).
    if len(state) >= SAFETY_MIN_STATE:
        # Count how many state UIDs should exist on each side
        state_on_carddav = sum(1 for s in state.values() if s.get("carddav_href"))
        state_on_google = sum(1 for s in state.values() if s.get("google_res"))

        if state_on_carddav >= SAFETY_MIN_STATE and len(c_contacts) < state_on_carddav * 0.30:
            log.error(
                f"SAFETY ABORT: CardDAV returned {len(c_contacts)} contacts but state expects ~{state_on_carddav}. "
                "Possible API/auth failure — refusing to sync to prevent mass deletions."
            )
            db.close()
            return

        if state_on_google >= SAFETY_MIN_STATE and len(g_linked) < state_on_google * 0.30:
            log.error(
                f"SAFETY ABORT: Google returned {len(g_linked)} linked contacts but state expects ~{state_on_google}. "
                "Possible API/auth failure — refusing to sync to prevent mass deletions."
            )
            db.close()
            return

    # 2. Total deletion check: if more than 20% of known contacts would be deleted
    state_uids_gone = [u for u in state if u not in g_linked and u not in c_contacts]
    if len(state) >= SAFETY_MIN_STATE and len(state_uids_gone) > len(state) * SAFETY_DELETE_PCT:
        log.error(
            f"SAFETY ABORT: {len(state_uids_gone)}/{len(state)} contacts would be deleted. "
            "Check API connectivity."
        )
        db.close()
        return

    stats = {"g_created": 0, "g_updated": 0, "c_created": 0, "c_updated": 0, "deleted": 0, "skipped": 0, "errors": 0}

    # 5. SYNC linked contacts (in state or matched by UID)
    for uid in all_uids:
        g = g_linked.get(uid)
        c = c_contacts.get(uid)
        s = state.get(uid)

        try:
            # --- DELETE: was in state, disappeared from one side ---
            if s and not g and not c:
                # Gone from both → just clean state
                if not DRY_RUN:
                    db.execute("DELETE FROM contacts WHERE uid=?", (uid,))
                stats["deleted"] += 1
                continue

            if s and not g and c:
                # Gone from Google → delete from CardDAV
                log.info(f"DELETE CardDAV (gone from Google): {uid}")
                if not DRY_RUN:
                    carddav.delete(c["href"])
                    db.execute("DELETE FROM contacts WHERE uid=?", (uid,))
                stats["deleted"] += 1
                continue

            if s and g and not c:
                # Gone from CardDAV → delete from Google
                res_name = s.get("google_res") or g["resourceName"]
                log.info(f"DELETE Google (gone from CardDAV): {uid}")
                if not DRY_RUN:
                    google.delete(res_name)
                    db.execute("DELETE FROM contacts WHERE uid=?", (uid,))
                stats["deleted"] += 1
                continue

            # --- CREATE: new on one side, no state ---
            if not s and c and not g:
                # New on CardDAV → create on Google
                log.info(f"CREATE Google ← CardDAV: {uid}")
                if not DRY_RUN:
                    body = vcard_to_google(c["vcard"], uid)
                    created = google.create(body)
                    fp = fingerprint_from_vcard(c["vcard"])
                    db.execute(
                        "INSERT INTO contacts (uid, google_res, carddav_href, etag_google, etag_carddav, fingerprint) VALUES (?,?,?,?,?,?)",
                        (uid, created["resourceName"], c["href"], created["etag"], c["etag"], fp),
                    )
                stats["g_created"] += 1
                continue

            if not s and g and not c:
                # New on Google → create on CardDAV
                log.info(f"CREATE CardDAV ← Google: {uid}")
                if not DRY_RUN:
                    vcard_str = google_to_vcard(g, uid)
                    href, etag = carddav.add(vcard_str)
                    fp = fingerprint_from_google(g)
                    db.execute(
                        "INSERT INTO contacts (uid, google_res, carddav_href, etag_google, etag_carddav, fingerprint) VALUES (?,?,?,?,?,?)",
                        (uid, g["resourceName"], href, g["etag"], etag, fp),
                    )
                stats["c_created"] += 1
                continue

            if not s and g and c:
                # Both exist but no state → already matched above or concurrent existence
                # Just link them
                log.info(f"LINK existing pair: {uid}")
                if not DRY_RUN:
                    fp = fingerprint_from_vcard(c["vcard"])
                    db.execute(
                        "INSERT OR IGNORE INTO contacts (uid, google_res, carddav_href, etag_google, etag_carddav, fingerprint) VALUES (?,?,?,?,?,?)",
                        (uid, g["resourceName"], c["href"], g["etag"], c["etag"], fp),
                    )
                stats["skipped"] += 1
                continue

            # --- UPDATE: both exist and are in state ---
            if s and g and c:
                g_etag = g.get("etag", "")
                c_etag = c.get("etag", "")
                s_g_etag = s.get("etag_google", "")
                s_c_etag = s.get("etag_carddav", "")

                g_changed = g_etag != s_g_etag
                c_changed = c_etag != s_c_etag

                if not g_changed and not c_changed:
                    stats["skipped"] += 1
                    continue

                if g_changed and not c_changed:
                    # Google wins
                    log.info(f"UPDATE CardDAV ← Google: {uid}")
                    if not DRY_RUN:
                        vcard_str = google_to_vcard(g, uid)
                        new_etag = carddav.put(c["href"], vcard_str, c["etag"])
                        db.execute(
                            "UPDATE contacts SET etag_google=?, etag_carddav=?, carddav_href=? WHERE uid=?",
                            (g_etag, new_etag, c["href"], uid),
                        )
                    stats["c_updated"] += 1

                elif c_changed and not g_changed:
                    # CardDAV wins
                    log.info(f"UPDATE Google ← CardDAV: {uid}")
                    if not DRY_RUN:
                        body = vcard_to_google(c["vcard"], uid)
                        res_name = s.get("google_res") or g["resourceName"]
                        updated = google.update(res_name, g_etag, body)
                        db.execute(
                            "UPDATE contacts SET etag_google=?, etag_carddav=? WHERE uid=?",
                            (updated["etag"], c_etag, uid),
                        )
                    stats["g_updated"] += 1

                else:
                    # Both changed — conflict. Use Google as winner (consistent with vdirsyncer).
                    log.warning(f"CONFLICT (Google wins): {uid}")
                    if not DRY_RUN:
                        vcard_str = google_to_vcard(g, uid)
                        new_etag = carddav.put(c["href"], vcard_str, c["etag"])
                        db.execute(
                            "UPDATE contacts SET etag_google=?, etag_carddav=?, carddav_href=? WHERE uid=?",
                            (g_etag, new_etag, c["href"], uid),
                        )
                    stats["c_updated"] += 1

        except Exception as e:
            log.error(f"Error processing {uid}: {e}")
            stats["errors"] += 1

    # 6. Handle truly unlinked Google contacts (no UID, no fingerprint match)
    for res_name, person in g_unlinked.items():
        try:
            new_uid = str(uuid.uuid4())
            log.info(f"CREATE CardDAV ← unlinked Google {res_name}: new UID {new_uid}")
            if not DRY_RUN:
                # Write UID to Google first
                body = vcard_to_google(google_to_vcard(person, new_uid), new_uid)
                body["etag"] = person["etag"]
                updated = google.update(res_name, person["etag"], body)

                # Then create on CardDAV
                vcard_str = google_to_vcard(person, new_uid)
                href, c_etag = carddav.add(vcard_str)
                fp = fingerprint_from_google(person)
                db.execute(
                    "INSERT INTO contacts (uid, google_res, carddav_href, etag_google, etag_carddav, fingerprint) VALUES (?,?,?,?,?,?)",
                    (new_uid, res_name, href, updated["etag"], c_etag, fp),
                )
            stats["c_created"] += 1
        except Exception as e:
            log.error(f"Error creating CardDAV for unlinked Google {res_name}: {e}")
            stats["errors"] += 1

    db.commit()
    db.close()

    log.info(
        f"Sync complete: "
        f"Google (+{stats['g_created']}, ~{stats['g_updated']}), "
        f"CardDAV (+{stats['c_created']}, ~{stats['c_updated']}), "
        f"deleted {stats['deleted']}, skipped {stats['skipped']}, "
        f"errors {stats['errors']}"
    )


if __name__ == "__main__":
    try:
        sync()
    except Exception as e:
        log.error(f"Fatal: {e}")
        sys.exit(1)
