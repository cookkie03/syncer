import os
import requests
import xml.etree.ElementTree as ET

url = "https://contacts.lucamanca.synology.me/carddav/LucaManca/"
user = os.environ.get("CALDAV_USERNAME")
pwd = os.environ.get("CALDAV_PASSWORD")

xml = '<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/><D:displayname/></D:prop></D:propfind>'
res = requests.request("PROPFIND", url, auth=(user, pwd), headers={"Depth": "1", "Content-Type": "application/xml"}, data=xml)

print(f"Status: {res.status_code}")
root = ET.fromstring(res.text)
collections = []
for resp in root.findall('.//{DAV:}response'):
    href = resp.find('{DAV:}href').text
    if href and href != "/carddav/LucaManca/":
        collections.append(href)

print(f"Found {len(collections)} items under root.")
if collections:
    print("Sample items:")
    for c in collections[:10]:
        print(f" - {c}")
