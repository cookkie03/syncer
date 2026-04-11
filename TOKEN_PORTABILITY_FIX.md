# Fix: Google OAuth Token Portability Across Devices

## Problem

When moving containers between devices (e.g., from PC to NAS), the Google OAuth token would fail with:

```
🚨 vdirsyncer sync FAILED
error: Unknown error occurred for caldavgcal/Lavoro: 401, message='Unauthorized'
```

This happened because:
1. Google OAuth `access_token` expires after **1 hour**
2. The `refresh_token` (which is long-lived) wasn't being used to refresh the expired token
3. vdirsyncer expects valid tokens but doesn't automatically refresh them

## Solution

The fix implements automatic token refresh before each sync using the `refresh_token`.

### Files Modified

| File | Change |
|------|--------|
| `vdirsyncer/token_refresh.py` | **NEW** - Python script that refreshes OAuth tokens |
| `vdirsyncer/sync-notify.sh` | Runs `token_refresh.py` before each sync |
| `vdirsyncer/entrypoint.sh` | Runs `token_refresh.py` at container startup |
| `vdirsyncer/Dockerfile` | Copies `token_refresh.py` into the container |
| `authorize-device.py` | Added documentation about `access_type=offline` |

## How It Works

1. **Token Structure**: The OAuth token file contains:
   - `access_token` - Valid for 1 hour
   - `refresh_token` - Long-lived (until revoked)
   - `expires_in` - When the access token expires

2. **Automatic Refresh**:
   - Before each sync, `token_refresh.py` checks if the token needs refresh
   - If expired (or expiring within 5 minutes), it uses the `refresh_token` to get a new `access_token`
   - The refreshed token is saved back to the token file

3. **Portable Tokens**: 
   - The token files now work on any device
   - The container can be moved without regenerating tokens
   - Tokens are automatically refreshed when needed

## Usage

### For New Tokens (recommended)

If you need to regenerate tokens to get a `refresh_token` (if your current tokens don't have one):

```bash
# On the new device, run:
./regenerate-token.sh

# Or manually:
python3 authorize-device.py
```

### Using Existing Tokens

If your existing tokens have a `refresh_token`, simply rebuild and restart the container:

```bash
docker-compose down
docker-compose build --no-cache vdirsyncer
docker-compose up -d vdirsyncer

# Check logs
docker-compose logs -f vdirsyncer
```

You should see output like:
```
[sync-notify] Refreshing OAuth tokens...
[token_refresh] Successfully refreshed token: google.json
[token_refresh] Successfully refreshed token: google_contacts.json
[token_refresh] Successfully refreshed token: google_gmail.json
```

## Technical Details

### OAuth Parameters (Critical)

For tokens to be refreshable, the authorization request must include:
```python
"access_type": "offline",  # Required to get a refresh_token
"prompt": "consent",       # Required to ensure refresh_token is returned
```

### Token Refresh Endpoint

```python
POST https://oauth2.googleapis.com/token
data={
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "refresh_token": REFRESH_TOKEN,
    "grant_type": "refresh_token"
}
```

Returns a new `access_token` (and potentially a new `refresh_token`).

## Verification

To verify your token has a `refresh_token`:

```bash
cat vdirsyncer/token/google.json | grep refresh_token
```

If empty, you need to regenerate the token with the updated authorization scripts.

## Notes

- The `refresh_token` is preserved across refreshes (Google doesn't always return a new one)
- Google may invalidate `refresh_token` if unused for 6 months or if account settings change
- If refresh fails, you'll need to re-run the authorization script