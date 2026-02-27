#!/bin/sh
# sync-notify.sh — wraps `vdirsyncer sync`, then sends a Telegram summary.
#
# Behaviour:
#   • On errors  : always notify with the full error lines + debug tracebacks.
#   • On success : notify once every NOTIFY_OK_EVERY_HOURS (default 24h) so
#                  you get a daily heartbeat without spam.
#
# Required env (optional — notifications are silently skipped if absent):
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
#
# Optional env:
#   NOTIFY_OK_EVERY_HOURS   heartbeat interval in hours (default: 24, 0 = never)
#   HEARTBEAT_FILE          path to the last-ok timestamp (default: /tmp/last_ok_notify)

set -e

HEARTBEAT_FILE="${HEARTBEAT_FILE:-/tmp/last_ok_notify}"
NOTIFY_OK_EVERY_HOURS="${NOTIFY_OK_EVERY_HOURS:-24}"
OUTPUT_FILE="/tmp/vdirsyncer_output"

# ── Run sync, capture all output ──────────────────────────────────────────────
# We always run with -v (verbose) so the output contains "Copying …" lines.
# If errors are found we re-run with -vdebug to get tracebacks (see below).
set +e
vdirsyncer sync > "$OUTPUT_FILE" 2>&1
EXIT_CODE=$?

# vdirsyncer 0.20.x has a concurrency bug where async Google Calendar sessions
# get closed mid-run. Retry only the specific failing collection(s) one at a
# time — no concurrency, so the race condition can't recur.
if [ "$EXIT_CODE" -ne 0 ] && grep -q "Session is closed" "$OUTPUT_FILE"; then
    RETRY_FILE="/tmp/vdirsyncer_retry"
    FAILED_FILE="/tmp/vdirsyncer_failed_collections"
    # Extract failing collection names, e.g. "caldav_gcal/Cura personale"
    # Use a file + while-read to preserve spaces in collection names
    grep "Session is closed" "$OUTPUT_FILE" \
        | sed 's/error: Unknown error occurred for \(caldav_gcal\/[^:]*\):.*/\1/' \
        | sort -u > "$FAILED_FILE"
    RETRY_EXIT=0
    while IFS= read -r COLLECTION; do
        echo "[sync-notify] Retrying $COLLECTION individually..."
        vdirsyncer sync "$COLLECTION" > "$RETRY_FILE" 2>&1
        COLL_EXIT=$?
        cat "$RETRY_FILE" >> "$OUTPUT_FILE"
        [ "$COLL_EXIT" -ne 0 ] && RETRY_EXIT=$COLL_EXIT
    done < "$FAILED_FILE"
    # Recalculate exit: 0 only if both the main run's other errors AND retries passed
    if [ "$RETRY_EXIT" -eq 0 ]; then
        # Check if there were non-session-closed errors in the main run
        OTHER_ERRORS=$(grep "^error:" "$OUTPUT_FILE" | grep -v "Session is closed" || true)
        [ -z "$OTHER_ERRORS" ] && EXIT_CODE=0
    fi
fi
set -e

OUTPUT=$(cat "$OUTPUT_FILE")
echo "$OUTPUT"   # still echo to Docker logs

# ── Parse output ──────────────────────────────────────────────────────────────
ERROR_LINES=$(printf '%s\n' "$OUTPUT" | grep "^error:" || true)
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
  # For connection/auth errors, re-run with -vdebug to grab tracebacks
  DEBUG_SECTION=""
  if [ "$HAS_DNS" -gt 0 ] || [ "$HAS_AUTH" -gt 0 ]; then
    set +e
    DEBUG_OUT=$(vdirsyncer --verbosity=DEBUG sync 2>&1 | grep -A6 "^error:\|ClientConnector\|Forbidden\|resolution" | head -60)
    set -e
    DEBUG_SECTION=$(printf '\n\n*Debug trace:*\n```\n%s\n```' "$DEBUG_OUT")
  fi

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
\`\`\`${HINTS}${DEBUG_SECTION}

_Exit code: ${EXIT_CODE} | Items copied this run: ${COPY_COUNT}_"

  telegram_send "$MSG"
  exit "$EXIT_CODE"
fi

# ── Notify on success (heartbeat) ────────────────────────────────────────────
if [ "$NOTIFY_OK_EVERY_HOURS" -gt 0 ]; then
  NOW=$(date +%s)
  LAST=0
  if [ -f "$HEARTBEAT_FILE" ]; then
    LAST=$(cat "$HEARTBEAT_FILE")
  fi
  ELAPSED_HOURS=$(( (NOW - LAST) / 3600 ))

  if [ "$ELAPSED_HOURS" -ge "$NOTIFY_OK_EVERY_HOURS" ]; then
    # Build a per-calendar summary line
    CALENDARS_SUMMARY=$(printf '%s\n' "$SYNC_LINES" | sed 's/^Syncing caldav_gcal\//  • /' || true)

    WARN_SECTION=""
    if [ -n "$WARN_LINES" ]; then
      WARN_SECTION=$(printf '\n\n*Warnings:*\n```\n%s\n```' "$(printf '%s\n' "$WARN_LINES" | head -10)")
    fi

    MSG="✅ *vdirsyncer sync OK*

*Calendars synced:*
${CALENDARS_SUMMARY}

Items copied: *${COPY_COUNT}*${WARN_SECTION}

_Next heartbeat in ${NOTIFY_OK_EVERY_HOURS}h_"

    telegram_send "$MSG"
    echo "$NOW" > "$HEARTBEAT_FILE"
  fi
fi
