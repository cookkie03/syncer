# Headless Authentication Guide

This document explains the supported Google authorization workflows for Syncer.

---

## Recommended flows

### 1. Headless host or NAS

Use:

```bash
python auth/authorize-device.py
```

This is the external-browser flow. The host prints:
- a verification URL
- a short user code

You then open the URL in a browser on another device, enter the code, approve access, and the host polls until the token is issued.

Recommended credential type in Google Cloud:
- `TVs and Limited Input devices`

Set these in `.env`:

```bash
GOOGLE_DEVICE_CLIENT_ID=...
GOOGLE_DEVICE_CLIENT_SECRET=...
```

## Token files

Successful authorization writes tokens into `vdirsyncer/token/`:

| File | Purpose | Used by |
|---|---|---|
| `vdirsyncer/token/google.json` | Google Calendar OAuth | `vdirsyncer` |
| `vdirsyncer/token/google_contacts.json` | Google Contacts OAuth | `google-contacts-backup` |

These files are bind-mounted into the containers at runtime.

This headless helper does not issue a Gmail token. Google documents limited-input
device flow for a restricted set of scopes, and `gmail.readonly` is not accepted
in this flow.

---

## Moving the project to a NAS

You can copy the project directory, including `vdirsyncer/token/`, onto the NAS and reuse the same tokens.

What must still be true:
- the OAuth client ID is still valid
- the refresh token has not been revoked or rotated by Google
- the APIs and scopes are still enabled

If Google returns `invalid_grant` or `invalidCredentials`, regenerate the token and recreate the affected containers.

---

## Recreate containers after new tokens

```bash
docker compose up -d --force-recreate vdirsyncer google-contacts-backup
```
