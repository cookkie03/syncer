#!/usr/bin/env python3
import os, sys, vobject, logging
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("restore")

def main():
    if len(sys.argv) < 2:
        print("Uso: python restore_from_vcf.py <path_al_file_vcf>")
        return

    vcf_path = Path(sys.argv[1])
    env = {}
    with open(Path(__file__).parent.parent / ".env", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k.strip()] = v.strip()

    token_file = Path(__file__).parent.parent / "vdirsyncer" / "token" / "google_contacts.json"
    creds = Credentials.from_authorized_user_file(token_file, ['https://www.googleapis.com/auth/contacts'])
    if creds.expired: creds.refresh(Request())
    service = build('people', 'v1', credentials=creds)

    log.info("Caricamento contatti esistenti da Google per evitare duplicati...")
    existing_names = set()
    page_token = None
    while True:
        res = service.people().connections().list(resourceName='people/me', pageSize=1000, pageToken=page_token, personFields='names').execute()
        for p in res.get('connections', []):
            if 'names' in p: existing_names.add(p['names'][0]['displayName'].lower().strip())
        page_token = res.get('nextPageToken')
        if not page_token: break

    log.info(f"Lettura file di backup: {vcf_path}")
    with open(vcf_path, 'r', encoding='utf-8') as f:
        vcf_content = f.read()

    # Dividi il file VCF in singoli contatti
    contacts = re.split(r'(?=BEGIN:VCARD)', vcf_content)
    restored = 0
    skipped = 0

    for v_str in contacts:
        if not v_str.strip(): continue
        try:
            v = vobject.readOne(v_str)
            name = v.fn.value.strip()
            if name.lower() in existing_names:
                log.debug(f"Saltato (già presente): {name}")
                skipped += 1; continue

            log.info(f"Ripristino: {name}")
            # Prepariamo il corpo per la People API
            body = {'names': [{'displayName': name}]}
            if hasattr(v, 'email_list'): body['emailAddresses'] = [{'value': e.value, 'type': 'home'} for e in v.email_list]
            if hasattr(v, 'tel_list'): body['phoneNumbers'] = [{'value': t.value, 'type': 'mobile'} for t in v.tel_list]
            if hasattr(v, 'uid'): body['externalIds'] = [{'value': v.uid.value, 'type': 'vCard-UID'}]
            
            service.people().createContact(body=body).execute()
            restored += 1
            time.sleep(0.2) # Evitiamo rate limit
        except Exception as e:
            log.error(f"Errore su contatto: {e}")

    log.info(f"Fine. Ripristinati: {restored}, Saltati: {skipped}")

import re, time
if __name__ == "__main__":
    main()
