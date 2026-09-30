"""One Google Desktop OAuth client for every Google integration."""

from __future__ import annotations

import json
from pathlib import Path


CLIENT_FILENAME = "client_secret.json"


def load_google_client(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    client = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(client, dict) or not client.get("client_id") or not client.get("client_secret"):
        raise ValueError(f"{path} must be a Google Desktop OAuth client JSON")
    return {"client_id": client["client_id"], "client_secret": client["client_secret"]}


def token_matches_client(
    payload: dict, client: dict[str, str], fields: tuple[str, ...], required_scope: str,
    saved_client_id: str | None = None,
) -> bool:
    raw_scope = (payload.get("scope") or payload.get("scopes")) if isinstance(payload, dict) else None
    scopes = raw_scope.split() if isinstance(raw_scope, str) else raw_scope
    return (
        isinstance(payload, dict)
        and all(payload.get(field) for field in fields)
        and (payload.get("client_id") or saved_client_id) == client["client_id"]
        and ("client_secret" not in payload or payload["client_secret"] == client["client_secret"])
        and isinstance(scopes, list)
        and required_scope in scopes
    )
