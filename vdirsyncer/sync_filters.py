from __future__ import annotations

import re


def extract_summary(item_raw: str) -> str:
    match = re.search(r"^SUMMARY(?:;[^:]*)?:(.*)$", item_raw, re.MULTILINE | re.IGNORECASE)
    if not match:
        return "(senza titolo)"
    summary = match.group(1).strip()
    return summary.replace(r"\,", ",").replace(r"\n", " ").replace(r"\\", "\\")


def is_skippable_google_write_error(storage_name: str, message: str) -> bool:
    if storage_name != "google_calendars":
        return False
    return "403" in message and "Forbidden" in message and "googleusercontent.com/caldav/v2/" in message


def format_skip_warning(action: str, ident: str, summary: str, message: str) -> str:
    compact = " ".join(message.split())
    return f"warning: Skipping {action} for Google event {ident} ({summary}): {compact}"
