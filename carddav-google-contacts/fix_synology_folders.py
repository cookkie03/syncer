#!/usr/bin/env python3
import os, sys, requests, re
from pathlib import Path
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

def main():
    # Caricamento credenziali con encoding esplicito per prevenire UnicodeDecodeError
    env = {}
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k.strip()] = v.strip()

    # URL RADICE (attenzione: puntiamo alla cartella LucaManca, non alla rubrica specifica)
    base_url = "https://contacts.lucamanca.synology.me/carddav/LucaManca/"
    user = env.get("CALDAV_USERNAME")
    pwd = env.get("CALDAV_PASSWORD")
    
    if not user or not pwd:
        print("Errore: Credenziali CALDAV non trovate nel file .env")
        return

    # L'ID della rubrica buona da NON cancellare
    MAIN_AB_ID = "eaa836e1-37e3-4fec-80dc-87ebaa82d9d8"

    s = requests.Session()
    s.auth = (user, pwd)

    print(f"Scansione rubriche su {base_url}...")
    xml = '<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/><D:displayname/></D:prop></D:propfind>'
    try:
        res = s.request("PROPFIND", base_url, data=xml, headers={"Depth": "1", "Content-Type": "application/xml"})
        res.raise_for_status()
    except Exception as e:
        print(f"Errore di connessione: {e}")
        return

    root = ET.fromstring(res.text)
    to_delete = []

    for resp in root.findall('.//{DAV:}response'):
        href_node = resp.find('{DAV:}href')
        if href_node is None: continue
        href = href_node.text
        
        # Se è una rubrica e NON è quella principale e NON è la radice
        if MAIN_AB_ID not in href and href != "/carddav/LucaManca/" and href != "/carddav/LucaManca":
            to_delete.append(urljoin(base_url, href))

    print(f"Trovate {len(to_delete)} rubriche errate da eliminare.")
    if not to_delete: 
        print("Nessuna rubrica da pulire.")
        return

    print("Procedo alla CANCELLAZIONE MASSIVA...")

    for idx, url in enumerate(to_delete):
        try:
            print(f"[{idx+1}/{len(to_delete)}] Eliminazione rubrica: {url}")
            s.delete(url).raise_for_status()
        except Exception as e:
            print(f"Errore su {url}: {e}")

    print("\nPulizia completata!")

if __name__ == "__main__":
    main()
