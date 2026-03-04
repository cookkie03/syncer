"""
Loader centralizzato per config.yaml.

Ordine di priorità: variabile d'ambiente > config.yaml > default nel codice.

Uso:
    from config_loader import cfg

    # Accesso a parametri di un servizio
    timeout = cfg("vtodo_notion.caldav_timeout", 60, int)

    # Accesso a parametri condivisi
    tg_timeout = cfg("shared.telegram_timeout", 10, int)

    # Parametro obbligatorio (nessun default)
    token = cfg.require_env("NOTION_TOKEN")
"""

import os
import sys
from pathlib import Path
from typing import Any

# PyYAML è opzionale: se non installato, funziona solo con env vars
try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

# ── Trova il config.yaml ──────────────────────────────────────────────────

_CONFIG_PATHS = [
    Path(os.environ.get("SYNCER_CONFIG", "")),               # override esplicito
    Path("/app/config.yaml"),                                 # nel container (montato)
    Path(__file__).resolve().parent.parent / "config.yaml",   # repo root (sviluppo locale)
]

_data: dict = {}

for p in _CONFIG_PATHS:
    if p.is_file():
        if yaml is None:
            print(f"[config] WARNING: trovato {p} ma PyYAML non installato — uso solo env vars", file=sys.stderr)
            break
        with open(p, encoding="utf-8") as f:
            _data = yaml.safe_load(f) or {}
        break


def _resolve(dotpath: str) -> Any:
    """Naviga config.yaml con notazione a punti: 'vtodo_notion.caldav_timeout'."""
    node = _data
    for key in dotpath.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _env_name(dotpath: str) -> str:
    """Converte dotpath in nome env var: 'vtodo_notion.caldav_timeout' → 'CALDAV_TIMEOUT'.
    Per i parametri shared: 'shared.telegram_timeout' → 'TELEGRAM_TIMEOUT'.
    """
    parts = dotpath.split(".")
    # Usa solo l'ultimo segmento (il nome del parametro), maiuscolo
    return parts[-1].upper()


def cfg(dotpath: str, default: Any = None, cast: type = str) -> Any:
    """
    Legge un parametro con priorità: env var > config.yaml > default.

    Args:
        dotpath: percorso nel YAML con punti (es. "vtodo_notion.caldav_timeout")
        default: valore di fallback se non trovato
        cast: tipo per il cast (int, float, str, bool)
    """
    # 1. Env var (massima priorità)
    env_key = _env_name(dotpath)
    env_val = os.environ.get(env_key)
    if env_val is not None:
        try:
            if cast is bool:
                return env_val.lower() in ("1", "true", "yes", "on")
            return cast(env_val)
        except (ValueError, TypeError):
            return default

    # 2. config.yaml
    yaml_val = _resolve(dotpath)
    if yaml_val is not None:
        try:
            return cast(yaml_val)
        except (ValueError, TypeError):
            return default

    # 3. Default
    return default


def require_env(name: str) -> str:
    """Legge una env var obbligatoria, esce se mancante."""
    val = os.environ.get(name)
    if not val:
        print(f"[config] ERRORE: variabile d'ambiente obbligatoria {name} non impostata", file=sys.stderr)
        sys.exit(1)
    return val


def env(name: str, default: str = "") -> str:
    """Legge una env var opzionale."""
    return os.environ.get(name, default)
