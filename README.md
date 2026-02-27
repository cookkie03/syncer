# caldav-sync

Self-hosted sync stack for Synology NAS (DSM 7.x) or any Windows/Linux/Mac machine running Docker.

| Service | What it does | Schedule |
|---|---|---|
| `vdirsyncer` | CalDAV VEVENT ↔ Google Calendar (bidirectional, GCal wins on conflict) | every 15 min |
| `vtodo-notion` | CalDAV VTODO ↔ Notion database (bidirectional) | every 10 min |
| `notion-backup` | Dual-track Notion backup: JSON via API + HTML ZIP via native export · hardlink snapshots · git versioning | daily (configurable) |
| `caldav-backup` | Full CalDAV backup (VEVENT + VTODO) exported as `.ics` files | every 60 min |

All services self-schedule via **supercronic** — no external cron or Task Scheduler needed.

---

## Prerequisites

- **Docker Engine ≥ 24** and **Docker Compose v2** (`docker compose`)
  - **Windows**: install [Docker Desktop](https://www.docker.com/products/docker-desktop/) — make sure it is running before any `docker` command
  - **Synology NAS (DSM 7.x)**: install **Container Manager** from Package Center
- A **Google Cloud project** with **Google Calendar API** enabled
- A **Notion account** with an internal integration token

---

## Step 1 — Configure `.env`

Copy the example file and fill in every value:

```bash
# Linux / Mac / Synology SSH
cp .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

The sections below explain where to find each value. Do not commit `.env` to git — it contains secrets.

---

## Step 2 — Google OAuth setup (vdirsyncer)

vdirsyncer syncs CalDAV calendars to Google Calendar via OAuth 2.0. This is a **one-time interactive step** that must be done on a machine with a browser (not via SSH).

### 2.1 — Enable APIs in Google Cloud Console

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create or select a project
3. Enable the **Google Calendar API** (APIs & Services → Library)

### 2.2 — Create OAuth credentials

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Application type: **Desktop app** — give it any name
3. Click **Create** — copy the `client_id` and `client_secret` into `.env`:
   ```
   GOOGLE_CLIENT_ID=your_client_id_here.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=GOCSPX-...
   ```

> If prompted to configure the OAuth consent screen, set it to **External**, add your Google account as a test user, and add the scope `https://www.googleapis.com/auth/calendar`.

### 2.3 — Run the authorization flow

vdirsyncer starts a local HTTP server on a **random port** and redirects the browser there to capture the OAuth code. Because Docker Desktop on Windows/Mac does not bridge random container ports to the host, the most reliable approach is to run `vdirsyncer authorize` **directly on the host machine** (not inside Docker), obtain the token file, then copy it into the Docker volume.

#### Option A — Run natively on Windows / Mac / Linux (recommended)

```bash
pip install "vdirsyncer[google]"
```

Create the config file from the template (replace variable values with what is in your `.env`):

```bash
# Linux / Mac
mkdir -p ~/.config/vdirsyncer
envsubst < vdirsyncer/config.template > ~/.config/vdirsyncer/config

# Windows PowerShell — manually copy config.template to:
# %USERPROFILE%\.config\vdirsyncer\config
# and replace $CALDAV_URL, $CALDAV_USERNAME, $CALDAV_PASSWORD,
# $CARDDAV_URL, $GOOGLE_TOKEN_FILE, $GOOGLE_CLIENT_ID, $GOOGLE_CLIENT_SECRET
# with the actual values from .env
```

Set the token output path and authorize:

```bash
# Linux / Mac
export GOOGLE_TOKEN_FILE="$HOME/google.json"
vdirsyncer authorize

# Windows PowerShell
$env:GOOGLE_TOKEN_FILE = "$HOME\google.json"
vdirsyncer authorize
```

The command opens a browser tab automatically. Log in, click **Allow**, and the terminal confirms `"Authorization successful."` — a `google.json` file is written to your home directory.

#### Option B — Run inside Docker (Linux hosts only)

On Linux (not Docker Desktop), the container shares the host network, so this works:

```bash
docker compose run --rm --network=host vdirsyncer vdirsyncer authorize
```

> **Synology NAS / SSH sessions**: SSH has no browser. Run Option A on your Windows/Mac machine first to get the token, then follow step 2.4 to copy it to the NAS volume.

### 2.4 — (Optional) Copy a pre-existing token into the volume

If you already have a `google.json` token from a previous local `vdirsyncer authorize`, you can load it into the Docker volume:

```bash
# Replace the volume name with your actual project folder prefix
docker run --rm \
  -v syncer_vdirsyncer_token:/data/token \
  -v "$HOME/google.json":/src/google.json \
  alpine cp /src/google.json /data/token/google.json
```

---

## Step 3 — Create the Notion database (vtodo-notion)

Create a new **full-page database** in Notion with this exact schema:

| Property name | Type | Options (exact spelling) |
|---|---|---|
| `Name` | Title | — |
| `UID CalDAV` | Text | — |
| `Descrizione` | Text | — |
| `Scadenza` | Date | — |
| `Priorità` | Select | `Alta`, `Media`, `Bassa`, `Nessuna` |
| `Luogo` | Text | — |
| `URL` | URL | — |
| `Lista` | Select | — (auto-populated from CalDAV list names) |
| `Periodicità` | Text | — |
| `Ultima sync` | Date | — |
| `Completato` | Status | `Done`, `In progress`, `Not started` |

**How `Completato` works:**
- Set it to **Done** in Notion → propagates `STATUS:COMPLETED` to CalDAV on the next sync
- **Non-recurring tasks**: the page is archived automatically on the next sync cycle
- **Recurring tasks** (field `Periodicità` non vuoto): the checkbox resets to `Not started` automatically and the due date advances to the next occurrence — the CalDAV server (Synology) manages the recurrence series

> **Synology Calendar — correct VTODO URL:**
> `CALDAV_URL` for `vtodo-notion` must point to the tasks endpoint, not the calendar endpoint.
> Example: `https://nas.example.com/caldav.php/username/home_todo/`
> (Use `/home_todo/` for VTODO lists, not `/home/` which is for VEVENT.)

### Get the database ID

Open the database in a browser. The URL looks like:
```
https://www.notion.so/yourworkspace/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx?v=...
```
The 32-character hex string before `?v=` is your `NOTION_DATABASE_ID`.

### Connect the integration

In the database, click **`...`** (top-right) → **Connections** → select your Notion integration.
Without this step the API token cannot read or write the database.

---

## Step 4 — Set up the Notion backup (notion-backup)

### 4.1 — Integration token (Track 1 — always active)

`NOTION_API_TOKEN` can be the **same value** as `NOTION_TOKEN`. Both are the same Notion integration secret (`ntn_...` or `secret_...`). Track 1 uses the official API and never expires.

### 4.2 — Browser cookies (Track 2 — HTML export, optional)

Track 2 exports the full Notion workspace as an HTML ZIP via Notion's internal API.
It needs two browser session cookies that **expire periodically** (weeks to a few months).

> **If Track 2 fails**, logs will show `[Track2] FAILED`. Track 1 always runs independently and is never blocked by Track 2. Renew the cookies below and restart the service.

#### How to get / renew `token_v2`

1. Open [notion.so](https://www.notion.so) in Chrome/Firefox — log in
2. Open **DevTools** (F12) → **Application** tab (Chrome) or **Storage** tab (Firefox)
3. Left panel: **Cookies → https://www.notion.so**
4. Find `token_v2` → copy its value → paste as `NOTION_TOKEN_V2` in `.env`

#### How to get / renew `file_token`

`file_token` is not in the static cookie list — it only appears in file download network requests:

1. Open [notion.so](https://www.notion.so) — log in
2. Open **DevTools** (F12) → **Network** tab
3. Navigate to a Notion page that has an image, PDF, or file attachment
4. In the Network tab, filter by `notion.so/f/`
5. Click one of those requests → **Headers** → **Request Headers**
6. Find the `cookie:` header → copy the `file_token=...` value (between `file_token=` and the next `;`)
7. Paste it as `NOTION_FILE_TOKEN` in `.env`

> **Alternative**: DevTools → Network → trigger an export from Notion UI (Settings → General → Export all workspace content) → find the `enqueueTask` request → Request Headers → `cookie:` → extract `file_token`.

#### How to get the Space ID (one-time setup)

1. DevTools → **Network** tab → reload notion.so
2. Filter requests by `api/v3` → click any request (e.g. `getSpaces`)
3. **Response** JSON → find key `"space"` → the first key inside is your space ID (32-char hex)
4. Paste it as `NOTION_SPACE_ID` in `.env`

#### After updating any token

```bash
docker compose restart notion-backup
docker compose logs notion-backup --tail=40
```

### 4.3 — Backup host path

Set `NOTION_BACKUP_PATH` to an **absolute path on the host** and create the directory first:

```bash
# Synology NAS (SSH)
mkdir -p /volume1/docker/syncer/notion-backup

# Linux / Mac
mkdir -p /opt/notion-backup

# Windows PowerShell
New-Item -ItemType Directory -Force "C:\notion-backup"
```

Then set in `.env`:
```
# Synology
NOTION_BACKUP_PATH=/volume1/docker/syncer/notion-backup

# Windows (forward slashes required in .env)
NOTION_BACKUP_PATH=C:/notion-backup
```

> The directory **must exist** before starting the container — Docker will fail to mount a non-existent bind path.

---

## Step 5 — CalDAV backup path

`caldav-backup` writes `.ics` files to `CALDAV_BACKUP_PATH`. Create the directory and set the path:

```bash
# Synology NAS (SSH)
mkdir -p /volume1/docker/syncer/caldav-backup

# Linux / Mac
mkdir -p /opt/caldav-backup

# Windows PowerShell
New-Item -ItemType Directory -Force "C:\caldav-backup"
```

Then in `.env`:
```
# Synology
CALDAV_BACKUP_PATH=/volume1/docker/syncer/caldav-backup

# Windows
CALDAV_BACKUP_PATH=C:/caldav-backup
```

---

## Step 6 — First run

```bash
# Build all images and start services in the background
docker compose up -d --build

# Follow all logs in real time
docker compose logs -f
```

Each service runs an initial sync/backup immediately on startup, then on its schedule.

---

## Operations

### Starting and stopping

```bash
# Start all services
docker compose up -d

# Stop all services (containers removed, data volumes kept)
docker compose down

# Restart a single service (e.g. after updating .env)
docker compose restart vtodo-notion

# Rebuild and restart after code changes
docker compose up -d --build vtodo-notion
```

### Checking logs

```bash
# All services, live
docker compose logs -f

# Single service, last 100 lines + live
docker compose logs -f --tail=100 vtodo-notion
docker compose logs -f --tail=100 vdirsyncer
docker compose logs -f --tail=100 notion-backup
docker compose logs -f --tail=100 caldav-backup

# Container health status
docker compose ps
```

### What to look for in logs

| Service | Healthy output | Warning signs |
|---|---|---|
| `vtodo-notion` | `Sync complete` · `errors=0` | `✗ ERROR` · `Circuit breaker triggered` · `Fatal sync error` |
| `vdirsyncer` | `Syncing caldav_gcal/...` (no `error:` lines) | `error:` · `401` / `403` · `name resolution` |
| `notion-backup` | `Tracks complete — JSON backup: OK` | `[Track1] Fatal` · `[Track2] FAILED` · `token_v2 or file_token may have expired` |
| `caldav-backup` | `Backup complete! Calendars: N` | `Error exporting` · `Required environment variable` |

### Verifying that sync actually happened

**vtodo-notion (CalDAV ↔ Notion tasks):**
- In Notion, the `Ultima sync` column is updated on every processed task — sort by it descending to confirm
- Successful sync log line:
  ```
  CalDAV → Notion: created=0, updated=2, skipped=45, archived=0, errors=0
  Notion → CalDAV: updated=1, skipped=46, archived=0, recurring_completed=0, errors=0
  ```

**vdirsyncer (CalDAV ↔ Google Calendar):**
- Open Google Calendar and check that events match your CalDAV server
- Telegram notification: you receive a `✅ vdirsyncer sync OK` heartbeat every 24 h (configurable via `NOTIFY_OK_EVERY_HOURS`)
- To force a manual sync: `docker compose exec vdirsyncer vdirsyncer sync`
- Sync runs every **15 minutes** (override in `docker-compose.yml` via `SYNC_INTERVAL_MINUTES`)

**notion-backup:**
- Check `NOTION_BACKUP_PATH/json/manifest.json` — it contains `timestamp` and `total_pages`
- A new git commit appears in `NOTION_BACKUP_PATH/.git` after each successful Track 1 run: `git -C /your/backup/path log --oneline -5`
- Backup runs at `BACKUP_SCHEDULE` (default `0 21 * * *` = 22:00 CET)

**caldav-backup:**
- Check `CALDAV_BACKUP_PATH/manifest.json` for `timestamp` and item counts
- `.ics` files are updated in-place every hour

### Telegram notifications

If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, all four services send alerts:

| Event | Who sends it |
|---|---|
| Sync errors > 20% of items | `vtodo-notion` |
| Circuit breaker activated | `vtodo-notion` |
| Fatal crash | `vtodo-notion` |
| Sync error (DNS, auth, etc.) | `vdirsyncer` |
| Daily heartbeat (sync OK) | `vdirsyncer` |
| Track 1 or Track 2 failed | `notion-backup` |

---

## Notion backup: dual-track strategy

### Why two tracks?

| | Track 1 — JSON (official API) | Track 2 — HTML ZIP (native export) |
|---|---|---|
| Auth | `NOTION_API_TOKEN` — **never expires** | Browser cookies — **may expire** |
| Output | Structured JSON per page/database | Full HTML, human-readable |
| Use case | Programmatic restore, diffs | Manual reading, disaster recovery |

Both tracks run **concurrently**. A failure in Track 2 (e.g. cookies expired) does not block Track 1.

### Snapshot retention

Hardlink snapshots of the JSON backup are created after each successful Track 1 run:

| Tier | Kept | Folder name | Policy |
|---|---|---|---|
| Daily | last 7 | `YYYY-MM-DD` | Overwritten every day |
| Weekly | last 8 | `YYYY-Www` | Created once per ISO week, never overwritten |

Hardlinks mean unchanged files share the same inode — extra disk usage per snapshot equals only what actually changed in Notion that day.

### Notion backup folder structure

```
NOTION_BACKUP_PATH/
├── json/                              ← always current (latest Track 1 output)
│   ├── manifest.json                  ← timestamp, page/db counts, all IDs + titles
│   ├── {page-id}/
│   │   ├── content.json               ← page metadata
│   │   └── blocks.json                ← full block tree
│   └── {database-id}/
│       ├── content.json
│       ├── blocks.json
│       └── rows.json                  ← all database rows
├── html/
│   ├── latest/                        ← latest Track 2 export, unzipped
│   └── archives/                      ← last 3 ZIPs (notion-export-YYYY-MM-DDTHH-MM-SSZ.zip)
├── snapshots/
│   ├── daily/
│   │   ├── 2025-01-17/                ← hardlink snapshot of json/ on that day
│   │   └── ... (last 7 days)
│   └── weekly/
│       ├── 2025-W03/
│       └── ... (last 8 weeks)
└── .git/                              ← git repo — one commit per successful backup
```

### Accessing Notion backups

The path at `NOTION_BACKUP_PATH` is a plain directory on your host filesystem:

| Platform | How to access |
|---|---|
| **Synology File Station** | Browse to `docker/syncer/notion-backup` |
| **Windows (SMB)** | `\\NAS-NAME\docker\notion-backup` |
| **Mac / Linux (SMB)** | `smb://NAS-NAME/docker/notion-backup` |
| **Windows local** | Explorer → `C:\notion-backup` |
| **SCP / rsync** | `rsync user@nas:/volume1/docker/syncer/notion-backup ./` |

---

## CalDAV backup folder structure

```
CALDAV_BACKUP_PATH/
├── calendar_NomCalendario.ics          ← all VEVENT for that calendar
├── tasks_NomeLista.ics                 ← all VTODO for that task list
└── manifest.json                       ← timestamp, calendar list, item counts
```

Files are overwritten in-place on every backup (hourly). There is no rotation — the backup is a snapshot of the current CalDAV server state.

---

## Configuration reference

| Variable | Service | Required | Default | Description |
|---|---|---|---|---|
| `CALDAV_URL` | vdirsyncer, vtodo-notion, caldav-backup | ✓ | — | CalDAV server root URL |
| `CARDDAV_URL` | vdirsyncer | ✓ | — | CardDAV server root URL |
| `CALDAV_USERNAME` | all | ✓ | — | CalDAV / CardDAV username |
| `CALDAV_PASSWORD` | all | ✓ | — | CalDAV / CardDAV password |
| `GOOGLE_CLIENT_ID` | vdirsyncer | ✓ | — | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | vdirsyncer | ✓ | — | Google OAuth client secret |
| `GOOGLE_TOKEN_FILE` | vdirsyncer | ✓ | `/data/token/google.json` | Do not change |
| `SYNC_INTERVAL_MINUTES` | vdirsyncer, vtodo-notion | — | `60` / `10` | Sync interval in minutes |
| `NOTION_TOKEN` | vtodo-notion | ✓ | — | Notion integration token (`ntn_...`) |
| `NOTION_DATABASE_ID` | vtodo-notion | ✓ | — | Target Notion database ID |
| `NOTION_SYNC_LOG_PATH` | vtodo-notion | — | `./logs-vtodo` | Host path for sync log file |
| `NOTION_API_TOKEN` | notion-backup | ✓ | — | Notion integration token (can equal `NOTION_TOKEN`) |
| `NOTION_TOKEN_V2` | notion-backup | — | — | Browser cookie for native HTML export |
| `NOTION_FILE_TOKEN` | notion-backup | — | — | Browser cookie for file downloads |
| `NOTION_SPACE_ID` | notion-backup | — | — | Notion workspace ID for native export |
| `NOTION_BACKUP_PATH` | notion-backup | ✓ | — | **Absolute host path** for backup storage (must exist) |
| `BACKUP_SCHEDULE` | notion-backup | — | `0 21 * * *` | Cron expression (UTC) — default = 22:00 CET |
| `GIT_REMOTE_URL` | notion-backup | — | — | Git remote to push backup repo after each commit |
| `CALDAV_BACKUP_PATH` | caldav-backup | — | `./caldav-backup-output` | Host path for CalDAV `.ics` backup files |
| `TELEGRAM_BOT_TOKEN` | all | — | — | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | all | — | — | Your Telegram user or chat ID |
| `TARGETARCH` | all (build) | ✓ | `amd64` | `amd64` (Intel/AMD) or `arm64` |

---

## Architecture notes

- All services schedule themselves via **supercronic** — no external cron needed
- `vtodo-notion` is **bidirectional**: conflict resolution is based on `last-modified` timestamp (most recent write wins)
- `vdirsyncer` is **bidirectional**: new/changed events propagate in both directions; when both sides differ simultaneously, **GCal wins** (`conflict_resolution = "b wins"`) — correct for shared meeting invitations where you are not the organizer. `My Calendar` (`l.manca03@gmail.com`) is excluded from sync to avoid 403 errors on read-only events.
- `notion-backup` Track 1 respects the Notion API rate limit (3 req/s, token-bucket)
- Snapshots use `unlink`-before-write: future writes to `json/` never corrupt inode of older snapshots
- Git commits happen only when Track 1 completes successfully

---

## Known issues / TODO

### vdirsyncer — Apple Reminders UIDs

Events created or modified via **Apple Reminders** (iOS/macOS) on a CalDAV calendar get stored with a non-standard UID of the form `x-apple-reminderkit://REMCDReminder/<UUID>`. vdirsyncer 0.20.x constructs a malformed Google Calendar API URL from this UID and fails with `Unknown error occurred`.

**Fix**: run the following one-time cleanup script to rewrite all Apple Reminders UIDs in-place to the plain UUID, then restart the container:

```bash
docker compose exec vdirsyncer python3 - << 'EOF'
import os, re, requests

URL  = os.environ["CALDAV_URL"].rstrip("/")
USER = os.environ["CALDAV_USERNAME"]
PASS = os.environ["CALDAV_PASSWORD"]
# ... (see project history for full script)
EOF
```

After fixing, vdirsyncer will sync those events to GCal on the next run.

### vdirsyncer — Outlook/Exchange "Busy" events with slash UIDs

Outlook meeting invitations sometimes produce base64-encoded UIDs containing `/` (e.g. `TOThHNg0/EOUGF2rrxm+0w==`). The slash splits the CalDAV API URL path and causes `Unknown error occurred`.

**Fix**: these are always empty "Busy" blocks — safe to delete from CalDAV. They will be automatically removed from GCal on the next sync.

---

## Future: Notion export auto-downloader via Gmail (not yet implemented)

**Goal**: a lightweight service (`notion-export-fetcher`) that monitors a Gmail label for Notion export-ready emails, extracts the download link, downloads the ZIP, and stores it alongside the Track 2 backup — fully automating what currently requires a manual step.

**Background**: since late 2024, Notion's internal export API no longer returns a direct `exportURL` in the task result. Instead, Notion sends an email to the account owner with a short-lived `file.notion.so/...` download link. Track 2 of `notion-backup` triggers the export but cannot capture the file automatically.

**Design**:
- Read-only Gmail access via OAuth 2.0 (scope: `gmail.readonly`) — never writes or deletes
- Poll a specific Gmail label (e.g. `Notion/exports`) every few minutes
- Parse email body (HTML or plain text) for links matching `https://file.notion.so/...`
- Download the ZIP using the same `file_token` cookie already in `.env`
- Save to `notion-backup/backup/html/archives/` with ISO timestamp filename
- Extract into `notion-backup/backup/html/latest/` (replacing previous latest)
- Send Telegram notification on success or download failure
- Mark processed emails to avoid re-downloading (via a local state file, not Gmail modification)

**Implementation notes**:
- Reuse `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` with scope `https://www.googleapis.com/auth/gmail.readonly`
- Separate OAuth token file for Gmail (`gmail.json` alongside `google.json` in `vdirsyncer/token/`)
- Label to watch: configurable via `GMAIL_NOTION_LABEL` in `.env`
- Download links expire after ~24 h — polling interval should be ≤ 1 h after first backup run of the day

## Future: Google Tasks mirror (not yet implemented)

**Goal**: a `vtodo-gtasks` container that mirrors VTODO state from CalDAV → Google Tasks (one-way, CalDAV is authoritative).

**Design**:
- CalDAV and Notion remain the two bidirectional sources of truth
- Google Tasks is a read-only mirror, useful for visibility in the Google ecosystem
- No sync back from Google Tasks (to avoid three-way conflict resolution)

**Implementation notes**:
- Use the [Google Tasks REST API](https://developers.google.com/tasks/reference/rest) (Google rejects VTODO over CalDAV)
- Reuse `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` with scope `https://www.googleapis.com/auth/tasks`
- One CalDAV VTODO list → one Google Tasks list; use VTODO `UID` as idempotent anchor in task notes
- Fields without Tasks equivalent (priority, RRULE, location): store as structured text in the `notes` field

---

## Future: Google Contacts sync (not yet implemented)

**Goal**: a `carddav-gcontacts` container that syncs CardDAV contacts ↔ Google Contacts bidirectionally.

**Why vdirsyncer `google_contacts` doesn't work**: Google deprecated CardDAV access for new OAuth apps in 2019. Even with a valid token, Google returns 403 on `.well-known/carddav`.

**Implementation plan**:
- Use the [Google People API](https://developers.google.com/people/v1/contacts) (REST, not CardDAV)
- Reuse `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, add scope `https://www.googleapis.com/auth/contacts`
- Map vCard fields → People API `Person` resource; store vCard `UID` in `Person.externalIds` for idempotent updates
- Cache `UID → resourceName` in a local SQLite file to handle Google's UID overwrite behavior

### ⚠️ Birthday date format — critical warning

vCard `BDAY` fields can arrive in three formats:

| Format | Example | Notes |
|---|---|---|
| `BDAY:19900115` | yyyymmdd compact | Most common |
| `BDAY:--0115` | day/month only, no year | vCard 3.0 standard |
| `BDAY:1990-01-15` | ISO 8601 with dashes | Less common |

**Risk**: some apps confuse DD/MM vs MM/DD, producing swapped birthdays. The `carddav-gcontacts` service will include an automatic diagnostic that logs a `WARNING` for every ambiguous date and always normalizes output to the People API format `{year, month, day}`.
