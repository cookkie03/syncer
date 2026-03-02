# vtodo-notion Refactor: Snapshot & Reconcile Design

## Problem

The current sync.py (~1100 lines) has accumulated patches that make it fragile:
- Ping-pong loops from timestamp-based change detection
- No deletion propagation (orphan tasks accumulate)
- Stale state file (473 UIDs tracked, only ~118 active)
- Complex recurring task handling (DUE advancement + restoration + force_update)
- Duplicate UID handling bolted on as afterthought
- Mixed indentation from successive edits

## Requirements (confirmed with user)

1. **Bidirectional sync** — modifications on either side propagate to the other; most recent wins
2. **Deletion propagation** — deleting from CalDAV archives on Notion; deleting from Notion deletes from CalDAV
3. **Completion handling** — completing non-recurring on CalDAV → archive Notion; completing recurring → advance DUE, stay active
4. **Scope** — sync all CalDAV lists that contain VTODO items (skip empty collections)
5. **Recurring cleanup** — auto-delete completed recurring VTODOs older than 10 days from CalDAV
6. **Robustness over speed** — prefer correctness and simplicity over optimization

## Architecture: Snapshot & Reconcile

### Core Algorithm

Each sync cycle:

1. **Snapshot CalDAV** — fetch all VTODOs from all collections → `dict[UID, TaskData]`
2. **Snapshot Notion** — fetch all pages from database → `dict[UID, TaskData]`
3. **Categorize UIDs** into 4 buckets:
   - **Both** — UID exists in CalDAV AND Notion
   - **CalDAV-only** — UID in CalDAV but not Notion
   - **Notion-only** — UID in Notion but not CalDAV
   - **Vanished** — UID in previous state but gone from both (cleanup state)
4. **Reconcile each bucket** — apply rules (see below)
5. **Update state** — save new snapshot of all known UIDs + content hashes
6. **Cleanup** — prune completed recurring VTODOs > 10 days old

### Reconciliation Rules

#### Both sides have the UID

```
content_hash(caldav) == content_hash(notion) → SKIP (identical)
caldav.last_modified > notion.last_edited    → UPDATE Notion from CalDAV
notion.last_edited > caldav.last_modified    → UPDATE CalDAV from Notion
```

Special case — recurring task COMPLETED on CalDAV:
- Do NOT mark as completed in Notion
- Recalculate DUE from RRULE, show as active with next occurrence date

Special case — recurring task completed on Notion:
- Advance DUE to next RRULE occurrence on CalDAV
- Reset Notion checkbox to "Not started"
- Do NOT write STATUS:COMPLETED to CalDAV

#### CalDAV-only (not in Notion)

```
UID in previous state (known_uids) → was DELETED from Notion → DELETE from CalDAV
UID NOT in previous state           → is NEW on CalDAV        → CREATE in Notion
```

#### Notion-only (not in CalDAV)

```
UID in previous state (known_uids) → was DELETED from CalDAV → ARCHIVE in Notion
UID NOT in previous state           → is NEW on Notion        → CREATE in CalDAV
```

#### First-run safety

If `known_uids` is empty (first deploy or state lost):
- Do NOT propagate deletions (everything looks "new")
- Only create and update
- Deletions activate from second cycle onward

### Data Model

```python
@dataclass
class TaskData:
    uid: str
    summary: str
    description: str
    due: str | None          # YYYY-MM-DD only
    priority: str            # Alta/Media/Bassa/Nessuna
    status: str              # In corso/Completato
    is_completed: bool
    location: str
    url: str
    rrule: str               # raw RRULE string
    list_name: str           # CalDAV collection name
    last_modified: str       # ISO timestamp for conflict resolution

    def content_hash(self) -> str:
        """Hash of semantic fields only (excludes timestamps)."""
        ...
```

```python
@dataclass
class SyncState:
    known_uids: dict[str, str]    # uid → content_hash (from last successful sync)
    last_sync: str | None         # ISO timestamp
```

### Field Mapping

| CalDAV VTODO | Notion Property | Type |
|---|---|---|
| UID | UID CalDAV | Rich Text |
| SUMMARY | Name | Title |
| DESCRIPTION | Descrizione | Rich Text (max 1990 chars) |
| DUE | Scadenza | Date (YYYY-MM-DD) |
| PRIORITY (0-9) | Priorita | Select (Alta/Media/Bassa/Nessuna) |
| LOCATION | Luogo | Rich Text |
| URL | URL | URL |
| STATUS | Completato | Status (Done/Not started) |
| RRULE | Periodicita | Rich Text |
| Calendar name | Lista | Select |

### Error Handling

- **Circuit breaker**: 5 consecutive API errors → stop current cycle, retry next scheduled run
- **Per-item errors**: log and continue; don't let one bad task block the rest
- **Telegram notifications**: only on fatal errors or circuit breaker activation
- **Retry with backoff**: 3 attempts with exponential backoff (1s, 2s, 4s) for transient failures

### Duplicate UID Handling

Pre-scan all CalDAV VTODOs before reconciliation:
- If same UID in multiple collections, prefer the NEEDS-ACTION instance over COMPLETED
- Log the duplicate with both collection names

### Recurring Task RRULE Engine

- `next_future_occurrence(rrule, base_due)`: compute next date >= today from RRULE
- Used in CalDAV→Notion to show correct future date
- Used in Notion→CalDAV when completing to advance DUE
- Fallback if RRULE exhausted: advance by 1 day + log warning

### File Structure

Single file `sync.py` (~500 lines), organized in sections:
1. Config & Logging
2. Data Model (TaskData, SyncState)
3. CalDAV Layer (fetch, parse, write, delete)
4. Notion Layer (fetch, parse, write, archive)
5. RRULE Engine
6. Reconciler (snapshot_diff, apply_changes)
7. Cleanup (completed recurring pruning)
8. Main (sync orchestrator)

### Deployment

No changes to Dockerfile, docker-compose.yml, or entrypoint.sh.
Same environment variables. Same cron schedule (*/10 minutes).
State file format changes but auto-migrates from old format on first run.
