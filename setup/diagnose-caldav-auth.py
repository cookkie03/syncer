"""One DSM login attempt with CalDAV credentials; never output secrets or SID."""
import json
import os
import urllib.request
import urllib.error
from urllib.parse import urlencode, urlsplit, urlunsplit


ERRORS = {
    400: "account inesistente o password errata",
    401: "account disabilitato",
    402: "permesso negato",
    403: "codice di autenticazione a due fattori richiesto",
    404: "codice di autenticazione a due fattori non valido",
    406: "autenticazione a due fattori obbligatoria",
    407: "indirizzo IP sorgente bloccato",
    408: "password scaduta non modificabile",
    409: "password scaduta",
    410: "cambio password obbligatorio",
}


def main():
    base = urlsplit(os.environ["CALDAV_URL"])
    if base.scheme != "https":
        print("Diagnosi DSM richiede HTTPS")
        return
    origin = urlunsplit((base.scheme, base.netloc, "", "", ""))

    class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urlsplit(newurl)
            if (target.scheme, target.netloc) != (base.scheme, base.netloc):
                raise RuntimeError("Redirect fuori dal NAS rifiutato")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(SameOriginRedirect())

    def post(path, parameters):
        request = urllib.request.Request(origin + path, data=urlencode(parameters).encode(),
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        with opener.open(request, timeout=20) as response:
            return json.load(response)

    sid = None
    auth_path = None
    try:
        info = post("/webapi/query.cgi", {"api": "SYNO.API.Info", "version": "1",
                                        "method": "query", "query": "SYNO.API.Auth"})
        if not info.get("success"):
            print("DSM API discovery: errore", info.get("error", {}).get("code"))
            return
        auth_path = info["data"]["SYNO.API.Auth"]["path"]
        if ":" in auth_path or ".." in auth_path or auth_path.startswith("/"):
            raise RuntimeError("Unexpected API path")
        auth_path = "/webapi/" + auth_path
        result = post(auth_path, {"api": "SYNO.API.Auth", "version": "6", "method": "login",
                                  "account": os.environ["CALDAV_USERNAME"],
                                  "passwd": os.environ["CALDAV_PASSWORD"], "format": "sid",
                                  "session": "SyncerDiagnostic"})
        if result.get("success"):
            sid = result.get("data", {}).get("sid")
            print("DSM login: riuscito (credenziali accettate)")
        else:
            code = result.get("error", {}).get("code")
            print(f"DSM login: codice {code}: {ERRORS.get(code, 'errore non classificato')}")
    except urllib.error.HTTPError as exc:
        print(f"DSM API: HTTP {exc.code}")
    except Exception as exc:
        print(f"DSM API: {type(exc).__name__}")
    finally:
        if sid and auth_path:
            try:
                result = post(auth_path, {"api": "SYNO.API.Auth", "version": "6", "method": "logout",
                                          "_sid": sid, "session": "SyncerDiagnostic"})
                print("Sessione diagnostica chiusa:", bool(result.get("success")))
            except Exception as exc:
                print("Chiusura sessione diagnostica:", type(exc).__name__)


if __name__ == "__main__":
    main()
