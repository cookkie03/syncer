# CalDAV Environment-Agnostic Endpoint Fallback

**Date:** 2026-03-10
**Status:** Approved
**Scope:** vtodo-notion, vdirsyncer, carddav-google-contacts

## Problem

The syncer system must run on both:
- **Synology NAS (production)**: CalDAV reachable via DDNS `https://calendar.lucamanca.synology.me/caldav/`
- **Windows/external dev machines**: Same DDNS hostname unreachable; only local fallback `http://host.docker.internal:5001/caldav/` works

Current state: Single `CALDAV_URL` hardcoded. On Windows, services fail with DNS resolution errors.

## Solution: Intelligent Endpoint Fallback

Services will attempt primary endpoint (DDNS) with configurable timeout, then automatically fallback to local endpoint if primary fails.

### Configuration

**`.env` changes:**
- Remove `CALDAV_TIMEOUT_SECONDS` (move to config.yaml)
- Add new variable: `CALDAV_URL_FALLBACK=http://host.docker.internal:5001/caldav/`
  - Used by: vtodo-notion, vdirsyncer, carddav-google-contacts
  - Can be empty string if user doesn't want fallback
  - Will be documented in `.env.example`

**`config.yaml` changes:**
- Add `caldav_timeout_seconds: 15` under `shared` section
- Individual services can override if needed
- Timeout applies to each connection attempt (primary and fallback each get full timeout)

### Connection Behavior

When connecting to CalDAV:

```
1. Try CALDAV_URL with timeout from config.yaml
   ├─ Success → continue with CALDAV_URL
   ├─ Timeout/DNS failure → proceed to step 2
   └─ Other error → propagate error

2. Try CALDAV_URL_FALLBACK (if defined) with same timeout
   ├─ Success → continue with CALDAV_URL_FALLBACK
   ├─ Timeout/failure → raise original error
   └─ Log which endpoint was used
```

### Affected Services

1. **vtodo-notion** (`sync.py`): CalDAV client initialization
2. **vdirsyncer** (`vdirsyncer.conf`): CalDAV pairs configuration
3. **carddav-google-contacts** (`sync_carddav.py`): CardDAV client initialization

### Implementation Details

**New module:** `shared/caldav_client.py`
- `class CalDAVConnectionConfig`: Holds URLs, timeout, fallback logic
- `connect_caldav(config) -> caldav.DAVClient`: Attempts primary, falls back to secondary
- Logging: Emit DEBUG log when fallback is used, so operator knows which endpoint is active

**Integration points:**
- vtodo-notion: Replace direct `caldav.DAVClient()` calls with wrapper
- vdirsyncer: Inject fallback URLs into `vdirsyncer.conf` at runtime (or modify config loading)
- carddav-google-contacts: Same pattern as vtodo-notion

### Backward Compatibility

- If `CALDAV_URL_FALLBACK` is empty/unset: use only primary endpoint (current behavior)
- Existing deployments continue to work without changes
- New deployments get automatic fallback for robustness

### Testing

- **Windows dev**: Start containers, verify services attempt DDNS then fallback to local
- **Synology prod**: Verify primary endpoint used immediately (no fallback overhead)
- **Broken primary**: Simulate DDNS unreachable, confirm fallback works
- **Broken both**: Verify appropriate error messages

### Success Criteria

✓ Services work on Windows development (DDNS fails, fallback succeeds)
✓ Services work on Synology production (DDNS works, no fallback delay)
✓ Timeout is configurable in config.yaml (default 15 seconds)
✓ Logging indicates which endpoint was used
✓ No hardcoded host.docker.internal — all configurable via .env
✓ If fallback URL empty, behavior unchanged from current
