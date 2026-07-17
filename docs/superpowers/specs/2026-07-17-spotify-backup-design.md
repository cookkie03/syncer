# Spotify Backup Design

Date: 2026-07-17
Scope: `spotify-backup` only
Status: Approved for implementation pending final user review of this spec

## Goal

Make `spotify-backup` run as a dedicated `docker compose` service that:

- runs an initial backup on container start
- runs scheduled backups every 4 hours
- persists logs and OAuth cache in the project folder
- writes a single mirror snapshot of the current Spotify state
- never requires browser-based auth inside the container or on the NAS
- remains portable when the project folder is copied to a Synology NAS

## Constraints

- This work is limited to `spotify-backup` and the `spotify-backup` service definition in `docker-compose.yml`.
- Runtime validation and tests must target only `spotify-backup`.
- Authentication must be initiated from an external device with a browser, not from the container.
- The backup must behave as a mirror of current Spotify state, not as an append-only history.
- The copied project folder must remain usable on another host without code changes, assuming required environment variables and cached tokens are present.

## External Requirements

Spotify currently supports `Authorization Code`, `Authorization Code with PKCE`, and `Client Credentials`. There is no native device-code flow for Spotify user data. Because this service needs user resources, the design uses `Authorization Code with PKCE`.

Spotify also announced refresh token expiration on 2026-06-18. Refresh tokens now expire 6 months after user authorization for new apps, and the same rule applies to existing apps starting 2026-07-20. The service must therefore treat cached tokens as portable but not permanent.

## Section 1: Runtime and Persistence

### Service wiring

Add a `spotify-backup` service to `docker-compose.yml` with:

- build context `./spotify-backup`
- image name aligned with existing local images
- `restart: unless-stopped`
- `env_file: .env`
- service-local environment for backup interval, log dir, backup dir, and cache path
- bind mounts for persistent data and logs

### Volumes

The service will mount:

- `./spotify-backup/data:/data`
- `./spotify-backup/logs:/logs`

This makes the following state portable with the project folder:

- current mirror snapshot
- OAuth cache and refresh token
- service logs

### Scheduling

The container entrypoint will:

1. validate required environment variables
2. create required directories
3. run one immediate backup attempt
4. start a scheduler that runs every 4 hours

The scheduler must generate a valid cron expression for 240 minutes and must not mis-handle multi-hour or multi-day intervals.

## Section 2: Authentication Model

### Headless principle

The container will never open a browser and will never try to guide an interactive login flow. If credentials are missing or invalid, the service will fail clearly in logs and preserve the last valid snapshot.

### External PKCE flow

Authentication will be performed by running `spotify-backup/auth_helper.py` on a machine controlled by the user, outside the container. The helper will:

- generate a PKCE authorization request
- accept the callback on the machine where the helper is running
- exchange the code for tokens
- write the cache file into `spotify-backup/data/.cache`

Two supported usage modes are expected:

1. local-browser mode on the same machine as the helper, using a loopback redirect URI such as `http://127.0.0.1:9000/callback`
2. external-browser mode where a temporary HTTPS tunnel forwards the callback back to the helper machine

The implementation work will not embed or manage a tunnel. It will make the helper pathing portable and keep the callback handling compatible with an externally provided tunnel URL.

### Portability to NAS

Once `spotify-backup/data/.cache` exists, copying the project folder to a Synology NAS should allow the service to start without code changes, provided that:

- the same Spotify app credentials are configured
- the same redirect URI strategy remains valid for future reauthorization
- the copied cache file is present and readable

If the refresh token expires or Spotify returns `invalid_grant`, the remedy is a new external authorization run, not a service-side fallback.

## Section 3: Mirror Semantics, Error Handling, and Testing

### Mirror semantics

The backup output will stop using timestamped history files. Instead, each successful run will produce a single current-state snapshot, for example:

- `/data/backup/spotify_backup_current.json`

Each new successful run fully replaces the previous snapshot. The service does not create local history beyond logs.

### Atomic writes

The snapshot must be written atomically:

1. write to a temporary file in the same directory
2. fsync or close cleanly
3. rename into place

This prevents partial writes from corrupting the last valid mirror.

### Failure behavior

If a run fails because of API errors, missing cache, or expired tokens:

- the previous valid snapshot remains unchanged
- the error is logged clearly
- the container remains in scheduled mode unless startup validation fails before scheduling
- no interactive recovery is attempted inside the container

### Test scope

Validation is limited to `spotify-backup` and will include:

- build verification for the `spotify-backup` image
- compose config verification for the `spotify-backup` service
- startup verification that the service creates expected directories and logs
- end-to-end backup verification if a valid cache is available
- negative-path verification that missing or invalid auth fails clearly without deleting the previous snapshot

## Planned File Changes

- `docker-compose.yml`
  Add the `spotify-backup` service with dedicated volumes and environment.
- `spotify-backup/auth_helper.py`
  Replace machine-specific cache path logic with a repo-relative portable path.
- `spotify-backup/backup.py`
  Replace timestamped file retention with a single atomic mirror snapshot and clearer auth error handling.
- `spotify-backup/entrypoint.sh`
  Ensure initial backup, reliable 4-hour scheduling, and persistent logging.

## Non-Goals

- No changes to other services or their tests.
- No browser automation inside Docker.
- No long-term token lifecycle workaround beyond clear reauthorization handling.
- No tunnel provisioning or DNS automation in this phase.

## Acceptance Criteria

- `docker compose config` includes a valid `spotify-backup` service definition.
- `docker compose up --build spotify-backup` starts only the `spotify-backup` service.
- Logs are persisted under `spotify-backup/logs`.
- The backup output is a single mirror snapshot, not a history of timestamped files.
- The OAuth cache lives under `spotify-backup/data/.cache`.
- A copied project folder can run `spotify-backup` on a Synology NAS without code edits, subject to valid environment variables and a non-expired token cache.
