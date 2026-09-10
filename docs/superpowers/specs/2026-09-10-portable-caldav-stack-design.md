# Portable CalDAV Stack Design

## Goal

Make the existing `vdirsyncer`, `caldav-backup`, and `spotify-backup` services portable across Synology and other Docker hosts by keeping every runtime file inside the `syncer/` project directory, while preserving the ignored services in `docker-compose.yml` without starting them.

## Scope

Included:

- `vdirsyncer`
- `caldav-backup`
- `spotify-backup`
- Their Dockerfiles, entrypoints, runtime scripts, tests, compose mounts, and documentation

Excluded from runtime:

- `vtodo-notion`
- `notion-backup`
- `google-contacts-backup`

Excluded services remain in the compose file as commented blocks.

## Architecture

The existing three containers remain the only containers. Compose uses relative bind mounts rooted in the project directory:

- `vdirsyncer/config`, `vdirsyncer/status`, `vdirsyncer/token`, `vdirsyncer/logs`
- `caldav-backup/backup`, `caldav-backup/logs`
- `spotify-backup/data`, `spotify-backup/logs`

No named Docker volumes or host-specific absolute paths are used. Network resolution is delegated to Docker and the configured URLs; hardcoded DNS/IP overrides are removed from the active services.

## CalDAV backup behavior

`caldav-backup` performs an initial backup and then runs on a configurable interval. A run writes to a temporary directory inside the backup root, exports every discovered VEVENT and VTODO collection, writes a manifest with source collection metadata, item counts, timestamp, and SHA-256 checksums, then atomically promotes the completed snapshot to `latest`.

Existing snapshots are retained according to `CALDAV_BACKUP_RETENTION` and are removed only after a successful promotion. Any discovery, export, validation, or manifest error leaves the prior `latest` snapshot untouched and exits unsuccessfully. Empty collections are represented by valid empty ICS files.

A daily discovery pass is enabled independently from the backup interval and records the discovered collection inventory in the manifest/logs. Discovery does not mutate backup data.

## Spotify backup behavior

`spotify-backup` keeps OAuth cache, incremental playlist cache, latest JSON, and timestamped snapshots under its existing `/data` bind mount. The current JSON is written atomically, then copied into a timestamped snapshot with configurable retention. A failed API run does not replace the last successful backup.

## vdirsyncer behavior

The generated vdirsyncer config is written under the persistent project-mounted config directory. Status, OAuth tokens, config, and logs all survive container recreation. Scheduling and startup behavior remain configurable through environment variables. The service does not embed device-specific IP addresses.

## Verification

Tests cover:

- atomic promotion and preservation of the previous CalDAV backup on failure;
- complete VEVENT/VTODO export, including empty collections;
- manifest checksums and retention;
- daily discovery scheduling configuration;
- persistent vdirsyncer config path and compose mounts;
- atomic Spotify snapshots and retention.

Operational checks include `docker compose config`, focused pytest tests, and a compose-level inspection confirming only the three requested services are active.
