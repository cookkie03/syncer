# CalDAV Fallback Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development to execute this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement intelligent CalDAV endpoint fallback so services work on both Windows development and Synology production without configuration changes.

**Architecture:** A shared connection wrapper (`shared/caldav_client.py`) handles primary DDNS → fallback local logic with configurable timeouts. vtodo-notion, vdirsyncer, and carddav-google-contacts integrate this wrapper.

**Tech Stack:** Python `caldav` library, `requests`, environment variables, config.yaml, git.

---

## Chunk 1: Configuration Foundation

### Task 1: Update config.yaml with timeout parameter

**Files:**
- Modify: `config.yaml:14-19`

- [ ] **Step 1: Add caldav_timeout_seconds to shared section**

Open `config.yaml` and locate the `shared:` section (line 14). Add the timeout parameter right after `shared:`:

```yaml
shared:
  caldav_timeout_seconds: 15  # ← ADD THIS LINE
  dns:
    - "192.168.1.1"
```

- [ ] **Step 2: Verify the change**

Read the file and confirm `caldav_timeout_seconds: 15` appears under `shared:` and before `dns:`.

- [ ] **Step 3: Commit**

```bash
cd "C:\Users\lucam\My Drive\Github\syncer"
git add config.yaml
git commit -m "config: add caldav_timeout_seconds to shared section (default 15s)"
```

### Task 2: Update .env.example with fallback URL

**Files:**
- Modify: `.env.example:31-40`

- [ ] **Step 1: Add CALDAV_URL_FALLBACK to .env.example**

Open `.env.example` and find line 31 where `CALDAV_URL=YOUR_VALUE_HERE` is defined. After that line, add:

```env
CALDAV_URL=YOUR_VALUE_HERE

# Fallback endpoint if primary DDNS is unreachable (e.g., on Windows dev machines).
# On Synology NAS, this should be empty — the primary DDNS always works.
# On external machines, set to the local network address:
#   http://host.docker.internal:5001/caldav/  (Docker Desktop on Windows/Mac)
#   https://192.168.1.X:5001/caldav/          (External Linux, X = NAS IP)
CALDAV_URL_FALLBACK=YOUR_VALUE_HERE
```

- [ ] **Step 2: Remove CALDAV_TIMEOUT_SECONDS comment (if present)**

Search .env.example for `CALDAV_TIMEOUT_SECONDS` and remove it entirely (it now lives in config.yaml).

- [ ] **Step 3: Verify both URLs are documented**

Read the file and confirm both `CALDAV_URL` and `CALDAV_URL_FALLBACK` are present with clear documentation.

- [ ] **Step 4: Commit**

```bash
git add .env.example
git commit -m "config: add CALDAV_URL_FALLBACK to .env.example"
```

---

## Chunk 2: Create Shared CalDAV Connection Wrapper

### Task 3: Create shared/caldav_client.py with fallback logic

**Files:**
- Create: `shared/caldav_client.py`

- [ ] **Step 1: Create the caldav_client.py module**

Create a new file at `shared/caldav_client.py` with the following content:

```python
"""
CalDAV connection wrapper with intelligent fallback.

Attempts primary endpoint (DDNS) first, then falls back to local endpoint
if primary fails due to timeout or DNS resolution error.

Usage:
    from caldav_client import get_caldav_client

    client = get_caldav_client(
        username="user",
        password="pass",
        timeout=15,
        logger=log  # optional
    )
"""

import os
import logging
import time
from typing import Optional

import caldav


def get_caldav_client(
    username: str,
    password: str,
    timeout: int = 15,
    logger: Optional[logging.Logger] = None
) -> caldav.DAVClient:
    """
    Get a CalDAV client with intelligent fallback.

    Attempts to connect to primary CALDAV_URL. If it times out or fails with
    a DNS/connection error, automatically tries CALDAV_URL_FALLBACK (if set).

    Args:
        username: CalDAV username
        password: CalDAV password
        timeout: Connection timeout in seconds (default 15)
        logger: Optional logger for debug/info messages

    Returns:
        caldav.DAVClient instance

    Raises:
        ValueError: If primary URL fails and no fallback is configured
        Exception: If both endpoints fail
    """

    if logger is None:
        logger = logging.getLogger(__name__)

    primary_url = os.environ.get("CALDAV_URL", "").strip()
    fallback_url = os.environ.get("CALDAV_URL_FALLBACK", "").strip()

    if not primary_url:
        raise ValueError("CALDAV_URL environment variable is not set")

    # Attempt primary endpoint
    logger.debug(f"[CalDAV] Attempting primary endpoint: {primary_url}")
    try:
        client = caldav.DAVClient(
            url=primary_url,
            username=username,
            password=password,
            timeout=timeout
        )
        # Test the connection by doing a simple request
        client.re_login()
        logger.info(f"[CalDAV] Successfully connected to primary endpoint")
        return client

    except (TimeoutError, ConnectionError, OSError) as e:
        # TimeoutError: socket timeout
        # ConnectionError: network unreachable, refused
        # OSError: includes socket.gaierror (DNS resolution failed)
        logger.warning(
            f"[CalDAV] Primary endpoint failed (timeout/connection): {type(e).__name__}: {e}"
        )

    except Exception as e:
        # Catch other errors (auth failures, etc.) and re-raise immediately
        # Only fallback on transient network errors
        logger.error(f"[CalDAV] Primary endpoint failed with non-recoverable error: {e}")
        raise

    # Primary failed with transient error; try fallback
    if not fallback_url:
        raise ValueError(
            f"Primary CalDAV endpoint ({primary_url}) failed and no "
            "CALDAV_URL_FALLBACK is configured. Set CALDAV_URL_FALLBACK "
            "in .env to enable local fallback."
        )

    logger.info(
        f"[CalDAV] Primary endpoint failed; attempting fallback endpoint: {fallback_url}"
    )

    try:
        client = caldav.DAVClient(
            url=fallback_url,
            username=username,
            password=password,
            timeout=timeout
        )
        client.re_login()
        logger.info(f"[CalDAV] Successfully connected to fallback endpoint")
        return client

    except Exception as e:
        logger.error(f"[CalDAV] Fallback endpoint also failed: {type(e).__name__}: {e}")
        raise ValueError(
            f"Both primary ({primary_url}) and fallback ({fallback_url}) "
            f"endpoints failed: {e}"
        )
```

- [ ] **Step 2: Verify the file exists**

Confirm `shared/caldav_client.py` was created with all the code above.

- [ ] **Step 3: Commit**

```bash
git add shared/caldav_client.py
git commit -m "feat: add CalDAV fallback wrapper with intelligent endpoint selection"
```

---

## Chunk 3: Integrate Wrapper into vtodo-notion

### Task 4: Update vtodo-notion/sync.py to use the wrapper

**Files:**
- Modify: `vtodo-notion/sync.py:30-47` (imports and config loading)
- Modify: `vtodo-notion/sync.py:965` (CalDAV client initialization)

- [ ] **Step 1: Add import for CalDAV wrapper**

In `vtodo-notion/sync.py`, find the imports section (around line 30). After the line `from config_loader import cfg, require_env, env`, add:

```python
from caldav_client import get_caldav_client
```

- [ ] **Step 2: Load CALDAV_URL_FALLBACK as optional environment variable**

In the configuration section (after line 38 where `TELEGRAM_CHAT_ID` is defined), add:

```python
CALDAV_URL_FALLBACK = env("CALDAV_URL_FALLBACK", "")  # optional fallback
```

- [ ] **Step 3: Update CALDAV_TIMEOUT to read from config.yaml**

Find line 47: `CALDAV_TIMEOUT = cfg("vtodo_notion.caldav_timeout", 60, int)`

Change it to read from shared section instead:

```python
CALDAV_TIMEOUT = cfg("shared.caldav_timeout_seconds", 15, int)
```

- [ ] **Step 4: Replace DAVClient instantiation with wrapper**

Find line 965:
```python
client = caldav.DAVClient(url=CALDAV_URL, username=CALDAV_USERNAME, password=CALDAV_PASSWORD, timeout=CALDAV_TIMEOUT)
```

Replace it with:

```python
client = get_caldav_client(
    username=CALDAV_USERNAME,
    password=CALDAV_PASSWORD,
    timeout=CALDAV_TIMEOUT,
    logger=log
)
```

- [ ] **Step 5: Test the changes locally**

Run vtodo-notion in Docker on Windows to verify:
1. Primary DDNS endpoint fails (expected)
2. Fallback kicks in
3. Sync completes successfully

```bash
docker compose up vtodo-notion --build
# Watch logs for: "[CalDAV] Primary endpoint failed" then "[CalDAV] Successfully connected to fallback endpoint"
```

- [ ] **Step 6: Commit**

```bash
git add vtodo-notion/sync.py
git commit -m "feat(vtodo-notion): use CalDAV fallback wrapper for endpoint selection"
```

---

## Chunk 4: Integrate Wrapper into carddav-google-contacts

### Task 5: Update carddav-google-contacts/sync.py

**Files:**
- Modify: `carddav-google-contacts/sync.py:35-50` (config loading)
- Modify: `carddav-google-contacts/sync.py:329-360` (CardDAVClient class)

- [ ] **Step 1: Update CardDAVClient to use fallback wrapper**

In `carddav-google-contacts/sync.py`, find the `CardDAVClient.__init__` method (around line 330). Replace the direct `requests.Session()` connection with:

```python
from caldav_client import get_caldav_client

class CardDAVClient:
    def __init__(self):
        if not CARDDAV_URL:
            raise ValueError("CARDDAV_URL is missing in environment variables.")

        # Use caldav wrapper for connection, then extract username/password for session auth
        caldav_timeout = cfg("shared.caldav_timeout_seconds", 15, int)
        try:
            # Get a validated client via the wrapper
            client = get_caldav_client(
                username=os.environ["CALDAV_USERNAME"],
                password=os.environ["CALDAV_PASSWORD"],
                timeout=caldav_timeout,
                logger=log
            )
            # Now use the authenticated session
            self.session = client.session if hasattr(client, 'session') else requests.Session()
            self.session.auth = (os.environ["CALDAV_USERNAME"], os.environ["CALDAV_PASSWORD"])
        except Exception as e:
            log.error(f"Failed to connect to CardDAV: {e}")
            raise

        # Discover addressbook as before
        base = CARDDAV_URL if CARDDAV_URL.endswith("/") else CARDDAV_URL + "/"
        self.addressbook_url = self._discover_addressbook(base)
        log.info(f"Using addressbook: {self.addressbook_url}")
```

Actually, since `caldav.DAVClient` uses a session internally, simplify:

```python
class CardDAVClient:
    def __init__(self):
        if not CARDDAV_URL:
            raise ValueError("CARDDAV_URL is missing in environment variables.")

        # Get validated CalDAV client via fallback wrapper
        caldav_timeout = cfg("shared.caldav_timeout_seconds", 15, int)
        client = get_caldav_client(
            username=os.environ["CALDAV_USERNAME"],
            password=os.environ["CALDAV_PASSWORD"],
            timeout=caldav_timeout,
            logger=log
        )

        # Use the underlying requests session for CardDAV operations
        self.session = client.session
        self.addressbook_url = self._discover_addressbook(CARDDAV_URL)
        log.info(f"Using addressbook: {self.addressbook_url}")
```

- [ ] **Step 2: Add import for wrapper and cfg**

At the top of the file (after other imports), add:

```python
from caldav_client import get_caldav_client
```

- [ ] **Step 3: Verify cfg import exists**

Check that `from config_loader import cfg` is present (around line 35).

- [ ] **Step 4: Test the changes**

Run carddav-google-contacts in Docker on Windows to verify fallback works.

- [ ] **Step 5: Commit**

```bash
git add carddav-google-contacts/sync.py
git commit -m "feat(carddav-google-contacts): use CalDAV fallback wrapper for endpoint selection"
```

---

## Chunk 5: Handle vdirsyncer Configuration

### Task 6: Update vdirsyncer entrypoint to support fallback

**Files:**
- Modify: `vdirsyncer/entrypoint.sh`
- Reference: `vdirsyncer/config.template`

vdirsyncer uses a config file template with environment variable substitution. Since vdirsyncer's config language doesn't support fallback logic natively, we'll add a fallback strategy at the entrypoint level.

- [ ] **Step 1: Review vdirsyncer/entrypoint.sh**

Read `vdirsyncer/entrypoint.sh` to see how it generates the config. You'll likely see environment variable substitution.

- [ ] **Step 2: Add fallback logic to entrypoint**

If the config uses `$CALDAV_URL`, add a check: if connection fails, retry with `$CALDAV_URL_FALLBACK`. However, since vdirsyncer handles its own connection logic, the best approach is to **not modify vdirsyncer** in this iteration.

**Rationale:** vdirsyncer is a mature tool with its own error handling and retry logic. It will automatically retry on connection failures, and users on Windows dev machines should set `CALDAV_URL_FALLBACK` to a working endpoint (e.g., `http://host.docker.internal:5001/caldav/`) if they want vdirsyncer to work.

- [ ] **Step 3: Document vdirsyncer fallback behavior**

Add a note to `.env.example` that vdirsyncer doesn't use the Python wrapper but respects both `CALDAV_URL` and can be reconfigured to use `CALDAV_URL_FALLBACK` by editing the generated config or setting `$CALDAV_URL` to the fallback value directly.

- [ ] **Step 4: (Optional) Monitor vdirsyncer logs**

Run vdirsyncer on Windows and confirm it either:
- Succeeds with primary DDNS (unlikely on Windows dev)
- Fails gracefully and logs the error
- User can manually switch to fallback by setting `CALDAV_URL=$CALDAV_URL_FALLBACK`

No code change required. Document the workaround in README or comments.

---

## Chunk 6: Integration Testing

### Task 7: Test on Windows (Docker Desktop)

**Setup:**
- Ensure `.env` has:
  - `CALDAV_URL=https://calendar.lucamanca.synology.me/caldav/`
  - `CALDAV_URL_FALLBACK=http://host.docker.internal:5001/caldav/`

- [ ] **Step 1: Start Docker Compose**

```bash
cd "C:\Users\lucam\My Drive\Github\syncer"
docker compose up --build
```

- [ ] **Step 2: Monitor vtodo-notion logs**

Watch output for:
- `[CalDAV] Attempting primary endpoint: https://calendar.lucamanca.synology.me/caldav/`
- `[CalDAV] Primary endpoint failed (timeout/connection)`
- `[CalDAV] Successfully connected to fallback endpoint`

If you see these three lines in order, fallback is working.

- [ ] **Step 3: Verify sync completes**

Check that vtodo-notion syncs complete without fatal errors. Look for:
- `[sync] Sync completed` or similar success message
- No repeated BrokenPipeError

- [ ] **Step 4: Monitor carddav-google-contacts logs**

Same pattern as vtodo-notion. Verify fallback is used.

- [ ] **Step 5: Document test results**

If all services work with fallback, note the timestamps and which endpoint was used.

### Task 8: Prepare for Synology testing (dry run)

**Setup:**
- On Synology, `.env` should have:
  - `CALDAV_URL=https://calendar.lucamanca.synology.me/caldav/` (same as Windows)
  - `CALDAV_URL_FALLBACK=` (empty or commented out)

- [ ] **Step 1: Create a test branch for Synology deployment**

```bash
git branch test/caldav-fallback-synology
```

- [ ] **Step 2: Push to Synology NAS**

Pull the latest code on the NAS and run:

```bash
docker compose up --build
```

- [ ] **Step 3: Verify logs**

Check that on Synology:
- `[CalDAV] Successfully connected to primary endpoint` (DDNS works immediately)
- No fallback attempts
- No delay from timeouts

- [ ] **Step 4: Commit final test results**

```bash
git add docs/
git commit -m "docs: test results for CalDAV fallback on Windows and Synology"
```

---

## Chunk 7: Documentation & Cleanup

### Task 9: Update README with fallback instructions

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add fallback configuration section**

Add a section explaining how to set `CALDAV_URL` and `CALDAV_URL_FALLBACK`:

```markdown
### CalDAV Endpoint Configuration

The system automatically falls back from primary DDNS to a local endpoint if the primary is unreachable.

**On Synology NAS (production):**
- `CALDAV_URL=https://calendar.lucamanca.synology.me/caldav/`
- `CALDAV_URL_FALLBACK=` (leave empty)

**On Windows/external development machine:**
- `CALDAV_URL=https://calendar.lucamanca.synology.me/caldav/` (primary, for testing)
- `CALDAV_URL_FALLBACK=http://host.docker.internal:5001/caldav/` (fallback when primary times out)

Services will try the primary endpoint with a timeout of `caldav_timeout_seconds` (default 15 seconds in `config.yaml`). If it fails, they automatically retry with the fallback endpoint.

To adjust the timeout, edit `config.yaml`:
```yaml
shared:
  caldav_timeout_seconds: 15  # adjust as needed
```
```

- [ ] **Step 2: Verify documentation is clear**

Read the section and confirm it explains:
- What happens automatically (fallback logic)
- How to configure both URLs
- How to adjust timeout

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add CalDAV fallback configuration section to README"
```

### Task 10: Final verification and cleanup

- [ ] **Step 1: Run git log to verify all commits**

```bash
git log --oneline | head -10
```

Should show commits for:
- CalDAV fallback wrapper creation
- vtodo-notion integration
- carddav-google-contacts integration
- Test results
- README update

- [ ] **Step 2: Verify all files are committed**

```bash
git status
```

Should show `working tree clean` (no uncommitted changes).

- [ ] **Step 3: Create a summary commit**

```bash
git commit --allow-empty -m "refactor: CalDAV endpoint fallback implementation complete

This implementation enables the syncer to work on both Windows development
machines and Synology production systems without configuration changes.

Services now intelligently fall back from DDNS primary to local fallback
endpoint when primary is unreachable due to timeout or DNS resolution failure.

- added shared/caldav_client.py: unified fallback logic
- updated vtodo-notion/sync.py: uses wrapper
- updated carddav-google-contacts/sync.py: uses wrapper
- vdirsyncer: documented workaround (no code change needed)
- updated config.yaml and .env.example for new parameters
- test results: verified on Windows and Synology

Timeout is configurable (default 15 seconds) via config.yaml."
```

---

## Success Criteria

✅ All chunks committed with working code
✅ vtodo-notion connects via fallback on Windows
✅ carddav-google-contacts connects via fallback on Windows
✅ Tests pass on both Windows (fallback active) and Synology (primary active)
✅ No timeout delays on Synology (primary endpoint used immediately)
✅ README documents the fallback feature and configuration

---

## Next Steps (Post-Implementation)

1. **Deploy to Synology NAS** — Pull latest code and test in production
2. **Monitor logs** — Verify correct endpoints being used
3. **Adjust timeout if needed** — If fallback takes too long, reduce timeout in config.yaml

