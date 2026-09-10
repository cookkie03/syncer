#!/usr/bin/env python3
"""
notion-backup — Telegram bot that receives a Notion export ZIP and saves it.

Flow:
  1. User receives download link from download_export.py via Telegram
  2. User downloads the ZIP and sends it back to this bot
  3. Bot saves it to /backup/zip_exports/notion_export_latest.zip
"""

import logging
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

for _p in ["/shared", str(Path(__file__).resolve().parent.parent / "shared")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
from config_loader import cfg, env  # noqa: E402

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = env("TELEGRAM_CHAT_ID")

BACKUP_DIR         = Path(cfg("notion_backup.backup_dir", "/backup"))
ZIP_DIR            = BACKUP_DIR / "zip_exports"
EXPORT_FILENAME    = ZIP_DIR / "notion_export_latest.zip"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("notion-telegram-bot")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)

    # Security: only accept files from the authorized chat
    if chat_id != str(TELEGRAM_CHAT_ID):
        log.warning("Ignored document from unauthorized chat_id: %s", chat_id)
        return

    doc = update.message.document
    if not doc.file_name or not doc.file_name.endswith(".zip"):
        await update.message.reply_text("⚠️ Invia un file .zip.")
        return

    log.info("Receiving ZIP: %s (%.2f MB)", doc.file_name, doc.file_size / (1024 * 1024))
    await update.message.reply_text("⏳ Salvataggio in corso...")

    try:
        ZIP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = EXPORT_FILENAME.with_suffix(".tmp")

        file = await context.bot.get_file(doc.file_id)
        await file.download_to_drive(str(tmp_path))
        tmp_path.replace(EXPORT_FILENAME)

        size_mb = EXPORT_FILENAME.stat().st_size / (1024 * 1024)
        log.info("Saved export: %s (%.2f MB)", EXPORT_FILENAME, size_mb)
        await update.message.reply_text(
            f"✅ Export salvato! {EXPORT_FILENAME.name} — {size_mb:.1f} MB"
        )
    except Exception as exc:
        log.error("Failed to save ZIP: %s", exc)
        await update.message.reply_text(f"❌ Errore durante il salvataggio: {exc}")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set — bot cannot start.")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_CHAT_ID not set — bot cannot start.")
        sys.exit(1)

    log.info("=== notion-telegram-bot starting (polling) ===")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
