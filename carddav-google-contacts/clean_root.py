import os
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

def main():
    url = "https://contacts.lucamanca.synology.me/carddav/LucaManca/"
    user = os.environ.get("CALDAV_USERNAME")
    pwd = os.environ.get("CALDAV_PASSWORD")

    s = requests.Session()
    s.auth = (user, pwd)

    xml = '<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/><D:displayname/></D:prop></D:propfind>'
    res = s.request("PROPFIND", url, headers={"Depth": "1", "Content-Type": "application/xml"}, data=xml)

    root = ET.fromstring(res.text)
    to_delete = []
    
    for resp in root.findall('.//{DAV:}response'):
        href = resp.find('{DAV:}href').text
        if href and ".vcf" in href:
            full_url = urljoin(url, href)
            to_delete.append(full_url)

    print(f"Found {len(to_delete)} bad VCF items in root.")
    
    for item in to_delete:
        print(f"Deleting {item}")
        r = s.delete(item)
        if r.status_code not in (200, 204, 202, 207):
            print(f"Failed to delete {item}: {r.status_code}")
    print("Done")

if __name__ == "__main__":
    main()
