"""
vtodo-notion — Configurazione centralizzata.

Tutti i parametri operativi in un unico posto.
Ogni valore è sovrascrivibile via variabile d'ambiente con lo stesso nome.
"""

import os
from pathlib import Path

# ── Helper ────────────────────────────────────────────────────────────────

def _env(name: str, default, cast=str):
    """Legge una variabile d'ambiente, con cast e default."""
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return cast(val)
    except (ValueError, TypeError):
        return default


# ── Credenziali (obbligatorie) ────────────────────────────────────────────

CALDAV_URL        = os.environ["CALDAV_URL"]
CALDAV_USERNAME   = os.environ["CALDAV_USERNAME"]
CALDAV_PASSWORD   = os.environ["CALDAV_PASSWORD"]
NOTION_TOKEN      = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")


# ── Percorsi ──────────────────────────────────────────────────────────────

STATE_FILE = Path(_env("STATE_FILE", "/data/sync_state.json"))
LOG_DIR    = Path(_env("LOG_DIR", "/data/logs"))
LOG_FILE   = LOG_DIR / "sync.log"


# ── Scheduling ────────────────────────────────────────────────────────────

SYNC_INTERVAL_MINUTES = _env("SYNC_INTERVAL_MINUTES", 5, int)


# ── Resilienza ────────────────────────────────────────────────────────────

MAX_RETRIES              = _env("MAX_RETRIES", 3, int)
CIRCUIT_BREAKER_THRESHOLD = _env("CIRCUIT_BREAKER_THRESHOLD", 5, int)
RETRY_BACKOFF_FACTOR     = _env("RETRY_BACKOFF_FACTOR", 5, int)       # secondi × tentativo


# ── Timeout (secondi) ────────────────────────────────────────────────────

CALDAV_TIMEOUT    = _env("CALDAV_TIMEOUT", 60, int)
TELEGRAM_TIMEOUT  = _env("TELEGRAM_TIMEOUT", 10, int)


# ── Pulizia ricorrenti ───────────────────────────────────────────────────

RECURRING_CLEANUP_DAYS = _env("RECURRING_CLEANUP_DAYS", 5, int)


# ── Content hash ──────────────────────────────────────────────────────────

DESCRIPTION_MAX_CHARS = _env("DESCRIPTION_MAX_CHARS", 1990, int)
HASH_LENGTH           = _env("HASH_LENGTH", 16, int)


# ── Logging ───────────────────────────────────────────────────────────────

LOG_LEVEL_FILE   = _env("LOG_LEVEL_FILE", "DEBUG")
LOG_LEVEL_STDOUT = _env("LOG_LEVEL_STDOUT", "INFO")
LOG_DATE_FORMAT_FILE   = "%Y-%m-%dT%H:%M:%SZ"
LOG_DATE_FORMAT_STDOUT = "%H:%M:%S"


# ── Testi default (italiano) ─────────────────────────────────────────────

DEFAULT_TITLE    = _env("DEFAULT_TITLE", "(senza titolo)")
DEFAULT_PRIORITY = _env("DEFAULT_PRIORITY", "Nessuna")
DEFAULT_STATUS   = _env("DEFAULT_STATUS", "In corso")
ICAL_PRODID      = _env("ICAL_PRODID", "-//vtodo-notion//EN")
