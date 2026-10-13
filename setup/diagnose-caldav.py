"""Read-only endpoint probe; run inside the existing CalDAV container."""
import os
import base64
import urllib.request
import urllib.error
from urllib.parse import urlsplit, urlunsplit

import caldav


class DiagnosticClient(caldav.DAVClient):
    def request(self, url, method="GET", *args, **kwargs):
        parsed = urlsplit(str(url))
        target = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}{parsed.path}"
        try:
            response = super().request(url, method, *args, **kwargs)
        except Exception as exc:
            label = "HTTP 401" if isinstance(exc, caldav.lib.error.AuthorizationError) else type(exc).__name__
            print(f"  {method} {target}: {label}; auth={type(self.auth).__name__}", flush=True)
            raise
        print(f"  {method} {target}: HTTP {response.status}", flush=True)
        return response

base = urlsplit(os.environ["CALDAV_URL"])
body = '''<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'''

# Compare transport implementations without installing anything in the container.
credentials = base64.b64encode((os.environ["CALDAV_USERNAME"] + ":" +
                               os.environ["CALDAV_PASSWORD"]).encode("utf-8")).decode("ascii")
url = urlunsplit((base.scheme, base.netloc, base.path, "", ""))
print("\nConfronto HTTP standard Python / CalDAV (stesse credenziali)", flush=True)
request = urllib.request.Request(url, data=body.encode(), method="PROPFIND", headers={
    "Authorization": "Basic " + credentials, "Depth": "0",
    "Content-Type": "text/xml", "User-Agent": "python-caldav/3.4.0",
})
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        print(f"  urllib Basic: HTTP {response.status}", flush=True)
except urllib.error.HTTPError as exc:
    print(f"  urllib Basic: HTTP {exc.code}", flush=True)
except Exception as exc:
    print(f"  urllib Basic: {type(exc).__name__}", flush=True)

for path, auth_type in [(base.path, "basic")]:
    url = urlunsplit((base.scheme, base.netloc, path, "", ""))
    print(f"\nEndpoint {path}; caldav {getattr(caldav, '__version__', 'unknown')}; auth={auth_type or 'auto'}", flush=True)
    client = DiagnosticClient(url=url, username=os.environ["CALDAV_USERNAME"],
                              password=os.environ["CALDAV_PASSWORD"], timeout=20,
                              auth_type=auth_type)
    stage = "PROPFIND iniziale"
    try:
        response = client.propfind(url, body, depth=0)
        print(f"{path}: HTTP {response.status}", flush=True)
        stage = "principal()"
        principal = client.principal()
        print("  Principal trovato", flush=True)
        stage = "principal.calendars()"
        collections = principal.calendars()
        print(f"  Raccolte trovate: {len(collections)}", flush=True)
        for index, collection in enumerate(collections, 1):
            stage = f"raccolta {index}: nome"
            # This also exercises the lazy property read used by backup.py.
            collection.name
            stage = f"raccolta {index}: todos(include_completed=True)"
            todos = collection.todos(include_completed=True)
            print(f"  Raccolta {index}: {len(todos)} task leggibili", flush=True)
            stage = f"raccolta {index}: events()"
            events = collection.events()
            print(f"  Raccolta {index}: {len(events)} eventi leggibili", flush=True)
        print("  Lettura completa riuscita", flush=True)
        break
    except caldav.lib.error.AuthorizationError:
        print(f"{path}: HTTP 401 durante {stage}", flush=True)
    except Exception as exc:
        # Do not print response bodies, headers or credentials.
        print(f"{path}: {type(exc).__name__} durante {stage}", flush=True)
    finally:
        client.close()
