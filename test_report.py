import os, requests
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
xml = """<?xml version="1.0" encoding="utf-8"?>
<C:addressbook-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:prop>
    <D:getetag/>
    <C:address-data/>
  </D:prop>
</C:addressbook-query>"""
print(f"URL: {url}")
res = requests.request('REPORT', url, auth=(user,pwd), headers={'Depth':'1', 'Content-Type':'application/xml'}, data=xml)
print(res.status_code)
print(len(res.text))
if len(res.text) > 0: print(res.text[:500])
