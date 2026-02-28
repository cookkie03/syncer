#!/usr/bin/env python3
import os
import sys
import logging
import vobject
import requests
from pathlib import Path
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dedupe")

def get_fingerprint(vcard_str):
    try:
        v = vobject.readOne(vcard_str)
        name = str(getattr(v, 'fn', getattr(v, 'n', ''))).strip().lower()
        emails = [e.value.strip().lower() for e in getattr(v, 'email_list', [])]
        phones = [re.sub(r'[^0-9+]', '', p.value) for p in getattr(v, 'tel_list', [])]
        email = emails[0] if emails else ""
        phone = phones[0] if phones else ""
        return f"{name}|{email}|{phone}" if name else ""
    except: return ""

import re

def main():
    # Caricamento manuale .env
    env = {}
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k.strip()] = v.strip()

    url = env.get("CARDDAV_URL")
    user = env.get("CALDAV_USERNAME")
    pwd = env.get("CALDAV_PASSWORD")

    if not url or not user:
        print("Errore: Credenziali mancanti nel file .env")
        return

    session = requests.Session()
    session.auth = (user, pwd)

    log.info("Connessione a Synology CardDAV...")
    # Discover addressbook
    xml_disc = '<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/></D:prop></D:propfind>'
    res = session.request("PROPFIND", url, data=xml_disc, headers={"Depth": "1", "Content-Type": "application/xml"})
    res.raise_for_status()
    
    ab_url = url
    for resp in ET.fromstring(res.text).findall('.//{DAV:}response'):
        rt = resp.find('.//{DAV:}resourcetype')
        if rt is not None and rt.find('.//{urn:ietf:params:xml:ns:carddav}addressbook') is not None:
            ab_url = urljoin(url, resp.find('{DAV:}href').text)
            break

    log.info(f"Rubrica trovata: {ab_url}")
    
    # Fetch all
    xml_get = '<?xml version="1.0"?><D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav"><D:prop><D:getetag/><C:address-data/></D:prop></D:propfind>'
    res = session.request("PROPFIND", ab_url, data=xml_get, headers={"Depth": "1", "Content-Type": "application/xml"})
    res.raise_for_status()

    contacts = []
    for resp in ET.fromstring(res.text).findall('.//{DAV:}response'):
        href = resp.find('{DAV:}href').text
        if href.endswith('/'): continue
        vcard = resp.find('.//{urn:ietf:params:xml:ns:carddav}address-data').text
        contacts.append({"url": urljoin(ab_url, href), "vcard": vcard})

    log.info(f"Trovati {len(contacts)} elementi totali.")

    seen = {} # fingerprint -> url
    to_delete = []

    for c in contacts:
        fp = get_fingerprint(c["vcard"])
        if not fp: continue
        
        if fp in seen:
            to_delete.append(c["url"])
        else:
            seen[fp] = c["url"]

    log.info(f"Risultato: {len(seen)} contatti unici, {len(to_delete)} duplicati da eliminare.")

    if not to_delete:
        print("Nessun duplicato trovato.")
        return

    confirm = input(f"Vuoi procedere alla CANCELLAZIONE di {len(to_delete)} contatti su Synology? (digitare 'si' per confermare): ")
    if confirm.lower() != 'si':
        print("Operazione annullata.")
        return

    for idx, target_url in enumerate(to_delete):
        try:
            log.info(f"[{idx+1}/{len(to_delete)}] Eliminazione: {target_url}")
            session.delete(target_url).raise_for_status()
        except Exception as e:
            log.error(f"Errore eliminazione {target_url}: {e}")

    print("
Pulizia completata con successo!")

if __name__ == "__main__":
    main()
