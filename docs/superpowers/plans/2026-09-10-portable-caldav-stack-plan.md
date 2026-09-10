# Portable CalDAV Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing vdirsyncer, caldav-backup, and spotify-backup services portable and fully persistent inside the syncer project directory.

**Architecture:** Keep the three existing containers and change only their mounts, runtime paths, scheduling, and backup atomicity. CalDAV and Spotify create timestamped snapshots and a latest view under project-relative bind mounts; vdirsyncer persists its generated config alongside status, tokens, and logs.

**Tech Stack:** Docker Compose, Python 3.11/3.12, pytest, caldav, spotipy, supercronic.

---

### Task 1: Add focused CalDAV backup tests

**Files:**
- Create: `caldav-backup/test_backup.py`
- Modify: `caldav-backup/backup.py`

- [ ] Add tests for sanitization, empty ICS output, successful atomic promotion, manifest checksums, retention, and preservation of `latest` when an export fails.
- [ ] Run `pytest caldav-backup/test_backup.py -q` and confirm the new behavior is initially failing.
- [ ] Implement the smallest helper boundaries needed by the tests: snapshot staging, checksum generation, validation, promotion, and retention.
- [ ] Rerun the focused test file and require all tests to pass.

### Task 2: Implement CalDAV snapshots and daily discovery

**Files:**
- Modify: `caldav-backup/backup.py`
- Modify: `caldav-backup/entrypoint.sh`
- Modify: `config/config.yaml`
- Modify: `docker-compose.yml`

- [ ] Export all discovered VEVENT and VTODO collections, including empty collections.
- [ ] Write each run under a unique temporary directory and promote it atomically to `latest` only after all exports and checksums succeed.
- [ ] Add configurable `CALDAV_BACKUP_RETENTION`, `CALDAV_DISCOVER_INTERVAL_HOURS`, and `CALDAV_BACKUP_INTERVAL_SECONDS` with defaults suitable for a daily discovery and four-hour backup.
- [ ] Run discovery once at startup and once per day without changing backup contents.
- [ ] Keep the previous snapshot on any failure and return a nonzero status for manual runs.
- [ ] Run `pytest caldav-backup/test_backup.py -q`.

### Task 3: Persist vdirsyncer configuration and remove host coupling

**Files:**
- Create: `vdirsyncer/config/.gitkeep`
- Modify: `vdirsyncer/entrypoint.sh`
- Modify: `docker-compose.yml`
- Modify: `config/.env.example`
- Modify: `README.md`

- [ ] Add `VDIRSYNCER_CONFIG_DIR` with default `/data/config` and point `XDG_CONFIG_HOME` at its parent so the generated config persists in `vdirsyncer/config`.
- [ ] Mount config, status, token, and logs under relative project paths.
- [ ] Remove active fixed `extra_hosts` mappings and document that each device sets its own `CALDAV_URL` in the project `.env`.
- [ ] Preserve commented service definitions without activating or deleting them.
- [ ] Run `docker compose config` with a temporary non-secret environment file and inspect active services and mounts.

### Task 4: Add Spotify snapshots and retention

**Files:**
- Create: `spotify-backup/test_backup.py`
- Modify: `spotify-backup/backup.py`
- Modify: `spotify-backup/entrypoint.sh`
- Modify: `docker-compose.yml`
- Modify: `config/.env.example`
- Modify: `README.md`

- [ ] Add tests for atomic current output, timestamped snapshots, retention, and failure preservation.
- [ ] Add `SPOTIFY_SNAPSHOT_DIR` and `SPOTIFY_SNAPSHOT_RETENTION` defaults under `/data/backup/snapshots`.
- [ ] Snapshot only after a complete successful API export; keep the existing cache under `/data/.cache`.
- [ ] Run focused Spotify tests.

### Task 5: Documentation and verification

**Files:**
- Modify: `README.md`
- Modify: `TESTING.md` if required by existing conventions

- [ ] Document copy-to-Synology deployment, relative paths, required per-device `.env`, initial authorization, daily discovery, retention, restore inputs, and manual checks.
- [ ] Run focused pytest suites and `docker compose config`.
- [ ] Review `git diff --check` and `git status`.
- [ ] Commit with a message indicating work resumed, for example `chore: riprendi i lavori sullo stack syncer`.
