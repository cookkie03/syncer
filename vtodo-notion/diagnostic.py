#!/usr/bin/env python3
"""CalDAV VTODO Diagnostic Report — analyzes from sync log data."""

import re
import json
from collections import Counter, defaultdict
from datetime import date, timedelta

today = date.today()
one_week = today + timedelta(days=7)

log_file = "/data/logs/sync.log"

with open(log_file, "r") as f:
    all_lines = f.readlines()

# Find last SUCCESSFUL sync cycle (one that has CalDAV data)
sync_starts = []
for i, line in enumerate(all_lines):
    if "Starting bidirectional sync" in line:
        sync_starts.append(i)

if not sync_starts:
    print("No sync cycle found in log!")
    exit(1)

# Try from the most recent backwards to find one with actual data
last_start = None
cycle_lines = []
for start_idx in reversed(sync_starts):
    end_idx = len(all_lines)
    # Find the next cycle start after this one
    for s in sync_starts:
        if s > start_idx:
            end_idx = s
            break
    candidate = all_lines[start_idx:end_idx]
    # Check if this cycle has CalDAV data (debug lines with UIDs)
    has_data = any("[CalDAV" in line and "UID=" in line for line in candidate)
    if has_data:
        last_start = start_idx
        cycle_lines = candidate
        break

if not cycle_lines:
    print("No successful sync cycle with CalDAV data found!")
    exit(1)
print(f"Analyzing last sync cycle starting at line {last_start+1}")
print(f"Cycle has {len(cycle_lines)} lines")

# Extract CalDAV items from debug lines
# status can be multi-word like "In corso"
pattern = re.compile(
    r"\[CalDAV.Notion\] \[([^:]+):(\d+)/(\d+)\] "
    r"UID=(\S+) summary='(.+?)' status='([^']+)' rrule=(\S*) due=(\S*)"
)

items = []
seen_uids = Counter()
uid_items = defaultdict(list)

for line in cycle_lines:
    m = pattern.search(line)
    if m:
        list_name = m.group(1)
        uid = m.group(4)
        summary = m.group(5)
        status = m.group(6)
        rrule = m.group(7) if m.group(7) else None
        due_str = m.group(8) if m.group(8) else None

        item = {
            "list": list_name,
            "uid": uid,
            "summary": summary[:60],
            "status": status,
            "rrule": rrule,
            "due": due_str,
        }
        items.append(item)
        seen_uids[uid] += 1
        uid_items[uid].append(item)

print(f"\nTotal CalDAV VTODO items parsed from log: {len(items)}")

# === COLLECTIONS ===
list_counts = Counter(i["list"] for i in items)
print(f"\n{'='*70}")
print("COLLECTIONS")
print(f"{'='*70}")
for name, count in list_counts.most_common():
    print(f"  {name}: {count} items")

# === DUPLICATES ===
dupes = {uid: count for uid, count in seen_uids.items() if count > 1}
print(f"\n{'='*70}")
print(f"DUPLICATE UIDs ({len(dupes)} UIDs appear more than once)")
print(f"{'='*70}")
for uid, count in sorted(dupes.items(), key=lambda x: -x[1])[:20]:
    entries = uid_items[uid]
    lists = [e["list"] for e in entries]
    statuses = [e["status"] for e in entries]
    summaries = set(e["summary"] for e in entries)
    dues = set(e.get("due") for e in entries)
    print(f"  x{count} UID={uid[:55]}")
    print(f"       Lists: {lists}")
    print(f"       Statuses: {statuses}")
    print(f"       Summaries: {summaries}")
    print(f"       DUEs: {dues}")

# === RECURRING ===
recurring = [i for i in items if i.get("rrule")]
non_recurring = [i for i in items if not i.get("rrule")]

past_recurring = []
future_recurring = []
no_due_recurring = []
for r in recurring:
    due = r.get("due")
    if not due:
        no_due_recurring.append(r)
    else:
        try:
            d = date.fromisoformat(due[:10])
            if d < today:
                past_recurring.append(r)
            else:
                future_recurring.append(r)
        except Exception:
            no_due_recurring.append(r)

print(f"\n{'='*70}")
print(f"RECURRING TASKS: {len(recurring)}")
print(f"{'='*70}")
print(f"  DUE in the past: {len(past_recurring)}")
print(f"  DUE today/future: {len(future_recurring)}")
print(f"  No/invalid DUE: {len(no_due_recurring)}")

if past_recurring:
    past_recurring.sort(key=lambda x: x.get("due", ""))
    print(f"\n  Recurring with PAST DUE (oldest first):")
    for r in past_recurring[:25]:
        rr = (r.get("rrule") or "")[:30]
        print(f"    {r['due']:12} | {r['status']:12} | {rr:30} | {r['summary'][:35]} [{r['list']}]")

# === STATUS BREAKDOWN ===
status_count = Counter(i["status"] for i in items)
print(f"\n{'='*70}")
print("STATUS BREAKDOWN (all items)")
print(f"{'='*70}")
for s, c in status_count.most_common():
    print(f"  {s}: {c}")

# === COMPLETED non-recurring ===
completed_non_recurring = [i for i in non_recurring if i["status"] == "Completato"]
print(f"\n{'='*70}")
print(f"COMPLETED non-recurring (cleanup targets): {len(completed_non_recurring)}")
print(f"{'='*70}")
cl = Counter(i["list"] for i in completed_non_recurring)
for name, count in cl.most_common():
    print(f"  {name}: {count}")

# === COMPLETED recurring ===
completed_recurring = [i for i in recurring if i["status"] == "Completato"]
print(f"\n{'='*70}")
print(f"RECURRING + COMPLETED (transition state): {len(completed_recurring)}")
print(f"{'='*70}")
for rc in completed_recurring[:15]:
    rr = (rc.get("rrule") or "")[:30]
    print(f"  {rc['due']:12} | {rr:30} | {rc['summary'][:35]} [{rc['list']}]")

# === OVERDUE ===
overdue = []
for i in non_recurring:
    if i["status"] != "Completato" and i.get("due"):
        try:
            d = date.fromisoformat(i["due"][:10])
            if d < today:
                overdue.append(i)
        except Exception:
            pass
overdue.sort(key=lambda x: x.get("due", ""))
print(f"\n{'='*70}")
print(f"OVERDUE non-recurring (active, DUE < today): {len(overdue)}")
print(f"{'='*70}")
for o in overdue[:20]:
    print(f"  {o['due']:12} | {o['status']:12} | {o['summary'][:40]} [{o['list']}]")

# === DUE THIS WEEK ===
this_week = []
for i in non_recurring:
    if i["status"] != "Completato" and i.get("due"):
        try:
            d = date.fromisoformat(i["due"][:10])
            if today <= d <= one_week:
                this_week.append(i)
        except Exception:
            pass
this_week.sort(key=lambda x: x.get("due", ""))
print(f"\n{'='*70}")
print(f"DUE THIS WEEK ({today} to {one_week}): {len(this_week)}")
print(f"{'='*70}")
for t in this_week:
    print(f"  {t['due']:12} | {t['summary'][:45]} [{t['list']}]")

# === WEIRD UIDs ===
weird = [i for i in items if re.search(r"-\d{8,}$", i["uid"])]
print(f"\n{'='*70}")
print(f"UIDs WITH NUMERIC SUFFIXES (sync artifacts?): {len(weird)}")
print(f"{'='*70}")
for w in weird[:15]:
    print(f"  UID={w['uid'][:60]}")
    print(f"       {w['summary'][:40]} [{w['list']}] status={w['status']}")
    # Check if base UID also exists
    base_uid = re.sub(r"-\d{8,}$", "", w["uid"])
    if base_uid in uid_items:
        base = uid_items[base_uid][0]
        print(f"       BASE EXISTS: {base['summary'][:40]} [{base['list']}] status={base['status']}")

# === RRULE analysis ===
rrule_freqs = Counter()
rrule_raw = set()
for r in recurring:
    rr = r.get("rrule", "")
    for part in rr.split(";"):
        if part.startswith("FREQ="):
            rrule_freqs[part] += 1
    rrule_raw.add(rr)

print(f"\n{'='*70}")
print("RRULE FREQUENCIES")
print(f"{'='*70}")
for f, c in rrule_freqs.most_common():
    print(f"  {f}: {c}")

print(f"\nUNIQUE RRULE PATTERNS ({len(rrule_raw)}):")
for rr in sorted(rrule_raw):
    print(f"  {rr}")

# === Items without DUE ===
no_due_active = [i for i in items if not i.get("due") and i["status"] != "Completato"]
print(f"\n{'='*70}")
print(f"ACTIVE ITEMS WITHOUT DUE: {len(no_due_active)}")
print(f"{'='*70}")
for nd in no_due_active[:15]:
    rr = nd.get("rrule") or ""
    print(f"  {nd['summary'][:45]} [{nd['list']}] rrule={rr[:30]}")

# === Notion-side analysis from state file ===
try:
    with open("/data/sync_state.json") as f:
        state = json.load(f)
    notion_uids = set(state.get("notion_modified", {}).keys())
    caldav_uids = set(state.get("caldav_modified", {}).keys())
    active_uids = set(i["uid"] for i in items if i["status"] != "Completato")

    print(f"\n{'='*70}")
    print("CROSS-REFERENCE: CalDAV vs Notion vs State")
    print(f"{'='*70}")
    print(f"  UIDs in CalDAV (from log): {len(seen_uids)}")
    print(f"  UIDs in state.caldav_modified: {len(caldav_uids)}")
    print(f"  UIDs in state.notion_modified: {len(notion_uids)}")
    print(f"  Active CalDAV UIDs: {len(active_uids)}")

    # UIDs in Notion but not in CalDAV (orphans?)
    notion_only = notion_uids - set(seen_uids.keys())
    print(f"\n  UIDs in Notion state but NOT in CalDAV: {len(notion_only)}")
    for uid in list(notion_only)[:10]:
        print(f"    {uid[:60]}")

    # UIDs with numeric suffix in Notion
    notion_weird = [u for u in notion_uids if re.search(r"-\d{8,}$", u)]
    print(f"\n  Notion UIDs with numeric suffixes: {len(notion_weird)}")
    for u in notion_weird[:10]:
        base = re.sub(r"-\d{8,}$", "", u)
        in_caldav = base in seen_uids
        print(f"    {u[:60]}  (base in CalDAV: {in_caldav})")

except Exception as e:
    print(f"Could not load state file: {e}")

print(f"\n{'='*70}")
print("END OF REPORT")
print(f"{'='*70}")
