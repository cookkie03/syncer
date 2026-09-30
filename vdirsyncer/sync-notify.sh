#!/bin/sh
# sync-notify.sh — wraps `python3 /app/sync_wrapper.py sync`, then sends a Telegram summary.
#
# Behaviour:
#   • On errors  : notify with the captured error lines.
#   • On success : notify when events changed.
#
# Required env (optional — notifications are silently skipped if absent):
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

set -e

LOCK_DIR="/tmp/vdirsyncer-sync.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "error: another vdirsyncer sync is already running" >&2
  exit 1
fi
trap 'rmdir "$LOCK_DIR"' EXIT
rm -f /tmp/vdirsyncer_changed_names.txt /tmp/vdirsyncer_skipped_names.txt

OUTPUT_FILE="/tmp/vdirsyncer_output"

# ── Run sync, capture all output ──────────────────────────────────────────────
# We always run with -v (verbose) so the output contains "Copying …" lines.
# Failed pairing blocks the sync, including diagnostics.
set +e
PAIRING_OK=1
RETRIED_SESSION_ERROR=0
# Recheck the pairing before every sync. A failed or ambiguous pairing blocks
# all writes and is handled by the normal error notification below.
if python3 /app/refresh_pairing.py --write > "$OUTPUT_FILE" 2>&1 \
    && python3 /app/render_config.py >> "$OUTPUT_FILE" 2>&1 \
    && yes '' | vdirsyncer discover caldav_gcal >> "$OUTPUT_FILE" 2>&1; then
    python3 /app/sync_wrapper.py sync >> "$OUTPUT_FILE" 2>&1
    EXIT_CODE=$?
else
    EXIT_CODE=$?
    PAIRING_OK=0
    echo "error: calendar pairing failed; sync was not run" >> "$OUTPUT_FILE"
fi

# vdirsyncer 0.20.x has a concurrency bug where async Google Calendar sessions
# get closed mid-run. Retry only the specific failing collection(s) one at a
# time — no concurrency, so the race condition can't recur.
if [ "$PAIRING_OK" -eq 1 ] && [ "$EXIT_CODE" -ne 0 ] && grep -q "Session is closed" "$OUTPUT_FILE"; then
    RETRY_FILE="/tmp/vdirsyncer_retry"
    FAILED_FILE="/tmp/vdirsyncer_failed_collections"
    # Extract failing collection names, e.g. "caldav_gcal/Cura personale"
    # Use a file + while-read to preserve spaces in collection names
    sed -n 's/^error: Unknown error occurred for \(caldav_gcal\/[^:]*\):.*Session is closed.*/\1/p' "$OUTPUT_FILE" \
        | sort -u > "$FAILED_FILE"
    if [ -s "$FAILED_FILE" ]; then
        RETRY_EXIT=0
        while IFS= read -r COLLECTION; do
            echo "[sync-notify] Retrying $COLLECTION individually..."
            python3 /app/sync_wrapper.py sync "$COLLECTION" > "$RETRY_FILE" 2>&1
            COLL_EXIT=$?
            cat "$RETRY_FILE" >> "$OUTPUT_FILE"
            [ "$COLL_EXIT" -ne 0 ] && RETRY_EXIT=$COLL_EXIT
        done < "$FAILED_FILE"
        # Only a successful retry with no other errors can recover the run.
        if [ "$RETRY_EXIT" -eq 0 ]; then
            OTHER_ERRORS=$(grep "^error:" "$OUTPUT_FILE" | grep -v "Session is closed" || true)
            if [ -z "$OTHER_ERRORS" ]; then
                EXIT_CODE=0
                RETRIED_SESSION_ERROR=1
            fi
        fi
    fi
fi
set -e

OUTPUT=$(cat "$OUTPUT_FILE")
echo "$OUTPUT"   # still echo to Docker logs

# ── Parse output ──────────────────────────────────────────────────────────────
ERROR_LINES=$(printf '%s\n' "$OUTPUT" | grep "^error:" || true)
if [ "$RETRIED_SESSION_ERROR" -eq 1 ]; then
  ERROR_LINES=$(printf '%s\n' "$ERROR_LINES" | grep -v "Session is closed" || true)
fi
WARN_LINES=$(printf '%s\n'  "$OUTPUT" | grep "^warning:" || true)
COPY_COUNT=$(printf '%s\n'  "$OUTPUT" | grep -c "^Copying" || true)
SYNC_LINES=$(printf '%s\n'  "$OUTPUT" | grep "^Syncing" || true)

HAS_DNS=$(printf '%s\n' "$ERROR_LINES" | grep -c "name resolution\|Cannot connect\|connection refused" || true)
HAS_AUTH=$(printf '%s\n' "$ERROR_LINES" | grep -c "401\|403\|Unauthorized\|Forbidden\|token" || true)

# ── Telegram helper ───────────────────────────────────────────────────────────
telegram_send() {
  _msg="$1"
  if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
    return 0
  fi
  # Escape backtick characters to avoid Markdown parse errors
  _safe=$(printf '%s' "$_msg" | tr '`' "'")
  _json=$(printf '%s' "$_safe" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
  curl -s -X POST \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\":\"${TELEGRAM_CHAT_ID}\",\"text\":${_json},\"parse_mode\":\"Markdown\"}" \
    > /dev/null 2>&1 || true
}

# ── Notify on error ───────────────────────────────────────────────────────────
if [ -n "$ERROR_LINES" ] || [ "$EXIT_CODE" -ne 0 ]; then
  # Compose diagnostic hints
  HINTS=""
  if [ "$HAS_DNS" -gt 0 ]; then
    HINTS="${HINTS}
⚠️ *DNS / connection failure* — check CALDAV\_URL (wrong host, port, or network)"
  fi
  if [ "$HAS_AUTH" -gt 0 ]; then
    HINTS="${HINTS}
🔑 *Auth error (401/403)* — Google token may need refresh, or event has unsupported properties"
  fi

  MSG="🚨 *vdirsyncer sync FAILED*

*Errors:*
\`\`\`
$(printf '%s\n' "$ERROR_LINES" | head -20)
\`\`\`${HINTS}

_Exit code: ${EXIT_CODE} | Items copied this run: ${COPY_COUNT}_"

  telegram_send "$MSG"
  [ "$EXIT_CODE" -ne 0 ] || EXIT_CODE=1
  exit "$EXIT_CODE"
fi

# Record a completed sync even when there were no event changes.
mkdir -p /data/status
date -u +%s > /data/status/.last-success.tmp
mv /data/status/.last-success.tmp /data/status/last-success

# ── Notify on success (only if things changed) ─────────────────────────────
if [ "$COPY_COUNT" -gt 0 ] || [ -s "/tmp/vdirsyncer_changed_names.txt" ]; then
  # Build a per-calendar summary line
  CALENDARS_SUMMARY=$(printf '%s\n' "$SYNC_LINES" | sed 's/^Syncing caldav_gcal\//  • /' || true)

  WARN_SECTION=""
  if [ -n "$WARN_LINES" ]; then
    WARN_SECTION=$(printf '\n\n*Warnings:*\n```\n%s\n```' "$(printf '%s\n' "$WARN_LINES" | head -10)")
  fi

  CHANGED_NAMES=""
  if [ -s "/tmp/vdirsyncer_changed_names.txt" ]; then
    CHANGED_NAMES=$(printf '\n\n*Eventi aggiornati:*\n%s' "$(cat /tmp/vdirsyncer_changed_names.txt | sed 's/^/  - /')")
  fi

  MSG="✅ *vdirsyncer sync*

*Calendars synced:*
${CALENDARS_SUMMARY}

Items copied: *${COPY_COUNT}*${CHANGED_NAMES}${WARN_SECTION}"

  telegram_send "$MSG"
  # Clear changed names
  rm -f /tmp/vdirsyncer_changed_names.txt
fi
