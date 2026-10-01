# Todoist validation — 2026-10-01

The folder convention from “Convenzioni subfolder” is applied: mutable database/checkpoints in `state/`, immutable snapshots under `backup/snapshots/`, a relative `backup/current` link, shared content-addressed files in `backup/objects/`, and `logs/`. Retention is unlimited. Migration preserves original `data/` and aborts on conflicting destinations.

## Verified locally

- 50 Todoist tests passed on macOS and inside the final Python 3.12 Docker image.
- 9 preparation tests, Contacts 7, Spotify 14, and vdirsyncer 15 regression tests passed.
- Shell syntax, Python compilation, Compose configuration, portability checks, and diff whitespace checks passed.
- Real API authentication, full Sync followed by incremental Sync, completed-task backfill, exports, SQLite integrity, object checksums, and snapshot checksums passed.
- The real Linux arm64 Docker workflow built the service image, performed a startup backup, survived restart, and performed a scheduled backup. Three snapshots were published; container health was healthy. Attachment installation across separate state/backup mounts and content deduplication passed using local synthetic file bytes. Health, verify, and export commands ran inside Docker.
- The final image independently verified all three captured test snapshots. The scheduled test used one-minute intervals; the production default remains four hours in Europe/Amsterdam.

## Coverage and limits

Real runs report `partial`: Todoist returned HTTP 400 for the legacy archived-sections route and HTTP 403 for an older activity-history window. These gaps are recorded; the completed-task history backfill completed. Mocked tests cover unavailable resources and file-download failures; real account attachments were not available, so the container attachment test uses synthetic local bytes.

NAS deployment and operation, and an amd64 container run, have not been verified. No commits or pushes were made. The isolated test containers were removed after testing.

Private reproducible test evidence is preserved at `state/docker-test-w6a15srq/result.json`; its state, snapshots, logs and export remain in that folder. Run `python3 setup/check-todoist-docker.py` from the repository to repeat the isolated workflow with Docker active.
