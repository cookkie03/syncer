#!/usr/bin/env python3
"""
notion-backup — Find latest Notion export email and forward download link via Telegram.

Flow:
  1. Search Gmail for Notion export email
  2. Resolve the Mailgun tracking link to get the real file.notion.so URL
  3. Send the URL to the user via Telegram bot
  4. telegram_bot.py handles receiving the zip back from the user
"""

import base64
import logging
import os
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────
for _p in ["/shared", str(Path(__file__).resolve().parent.parent / "shared")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
from config_loader import cfg, env  # noqa: E402

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = env("TELEGRAM_CHAT_ID")
TOKEN_FILE         = env("GOOGLE_GMAIL_TOKEN_FILE", "/data/token/google_gmail.json")

BACKUP_DIR         = Path(cfg("notion_backup.backup_dir", "/backup"))
STATE_FILE         = BACKUP_DIR / ".last_notified_msg_id"

TELEGRAM_TIMEOUT   = cfg("shared.telegram_timeout", 10, int)

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("notion-download-export")


def notify(title: str, message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    text = f"*{title}*\n\n{message}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=TELEGRAM_TIMEOUT,
        )
    except Exception as exc:
        log.warning("[notify] Failed to send Telegram message: %s", exc)


def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise Exception(
                f"Gmail token not found or invalid at {TOKEN_FILE}. "
                "Please run authorize-google.py first."
            )
    return build("gmail", "v1", credentials=creds)


def find_latest_export_email(service) -> tuple[str, str] | None:
    queries = [
        'label:"[notion]" subject:export',
        "from:notion.so export",
    ]
    for query in queries:
        log.info("Searching Gmail with query: %s", query)
        results = service.users().messages().list(
            userId="me", q=query, maxResults=1
        ).execute()
        messages = results.get("messages", [])
        if messages:
            msg_id = messages[0]["id"]
            meta = service.users().messages().get(
                userId="me", id=msg_id, format="metadata",
                metadataHeaders=["Subject", "Date"]
            ).execute()
            headers = {h["name"]: h["value"] for h in meta.get("payload", {}).get("headers", [])}
            subject = headers.get("Subject", "(no subject)")
            date = headers.get("Date", "unknown date")
            log.info("Found email: [%s] %s — %s", msg_id, subject, date)
            return msg_id, subject
    log.warning("No Notion export emails found.")
    return None


def is_already_notified(msg_id: str) -> bool:
    if STATE_FILE.exists():
        return STATE_FILE.read_text().strip() == msg_id
    return False


def save_notified_state(msg_id: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(msg_id)


def get_email_links(service, msg_id: str) -> list[str]:
    message = service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()

    parts = message.get("payload", {}).get("parts", [])
    body = ""

    if not parts:
        data = message.get("payload", {}).get("body", {}).get("data")
        if data:
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    else:
        for part in parts:
            if part.get("mimeType") == "text/html":
                data = part.get("body", {}).get("data")
                if data:
                    body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    break
        if not body:
            for part in parts:
                if part.get("mimeType") == "text/plain":
                    data = part.get("body", {}).get("data")
                    if data:
                        body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                        break

    if not body:
        log.error("Could not find body in email %s", msg_id)
        return []

    soup = BeautifulSoup(body, "html.parser")
    return [a["href"] for a in soup.find_all("a", href=True)]


def find_export_link(links: list[str]) -> str | None:
    """
    Find the Mailgun tracking link that points to the Notion export download.
    Returns the original Mailgun URL (not the resolved one), because file.notion.so
    requires browser authentication — the Mailgun link works when opened in a browser
    where the user is logged into Notion.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    for href in links:
        try:
            # Follow redirects just to identify WHICH link is the download one
            resp = requests.get(
                href, allow_redirects=True, timeout=15,
                stream=True, headers=headers
            )
            final_url = resp.url
            resp.close()
            if "file.notion.so" in final_url or final_url.endswith(".zip"):
                log.info("Found export link: %s -> %s", href[:60], final_url[:80])
                return href  # Return the ORIGINAL Mailgun link, not the resolved one
        except Exception as exc:
            log.warning("Failed to check %s: %s", href[:60], exc)
    return None


def main():
    log.info("=== notion-download-export starting ===")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    try:
        service = get_gmail_service()
        result = find_latest_export_email(service)

        if not result:
            log.warning("No export email found — nothing to do.")
            return

        msg_id, subject = result

        if is_already_notified(msg_id):
            log.info("Email %s already notified — skipping.", msg_id)
            return

        links = get_email_links(service, msg_id)
        if not links:
            log.error("No links found in email.")
            return

        export_url = find_export_link(links)
        if not export_url:
            log.error("Could not resolve export URL from email links.")
            notify("Notion export: link non trovato", f"Email trovata ma nessun link valido.\nSoggetto: {subject}")
            return

        save_notified_state(msg_id)
        log.info("Sending download link via Telegram.")
        notify(
            "📦 Export Notion pronto!",
            f"Scarica lo ZIP dal link qui sotto, poi inviamelo su questa chat.\n\n{export_url}",
        )

    except Exception as exc:
        log.error("Export check failed: %s", exc)
        notify("Notion export: ERRORE", f"Errore durante il controllo email: {exc}")

    log.info("=== notion-download-export done ===")


if __name__ == "__main__":
    main()
