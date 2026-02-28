import os
import requests

def load_env(path=".env"):
    if not os.path.exists(path): return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

load_env()

url = os.environ.get('CARDDAV_URL')
user = os.environ.get('CALDAV_USERNAME')
pwd = os.environ.get('CALDAV_PASSWORD')

xml = '''<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
    <D:prop>
        <D:resourcetype/>
        <D:displayname/>
    </D:prop>
</D:propfind>'''

print(f"Querying {url}")
res = requests.request("PROPFIND", url, auth=(user, pwd), headers={'Depth': '1', 'Content-Type': 'application/xml'}, data=xml)
print(f"Status: {res.status_code}")
print(res.text[:1000])
