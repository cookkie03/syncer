# Spotify Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `spotify-backup` run as a compose-managed headless service with external PKCE auth, a single mirror snapshot, persistent logs, and validation limited to `spotify-backup`.

**Architecture:** Keep all runtime state inside `spotify-backup/data` and `spotify-backup/logs`, mount those paths through `docker-compose.yml`, and let the container consume an externally generated OAuth cache. Replace timestamped backup history with one atomic current-state JSON file and verify the behavior with targeted unit tests plus service-level compose checks.

**Tech Stack:** Docker Compose v2, Python 3.12, Spotipy, Bash, `unittest`

---

### Task 1: Add failing tests for mirror output and portable auth cache

**Files:**
- Create: `spotify-backup/test_backup.py`
- Create: `spotify-backup/test_auth_helper.py`
- Modify: `spotify-backup/backup.py`
- Modify: `spotify-backup/auth_helper.py`

- [ ] **Step 1: Write the failing tests**

```python
# spotify-backup/test_backup.py
def test_save_backup_replaces_single_current_snapshot_without_history(self):
    module = load_backup_module(self)
    module.BACKUP_DIR = str(self.backup_dir)

    first = module.save_backup({"timestamp": "2026-07-17T10:00:00"})
    second = module.save_backup({"timestamp": "2026-07-17T14:00:00"})

    self.assertEqual(first, str(self.backup_dir / "spotify_backup_current.json"))
    self.assertEqual(second, first)
    self.assertEqual(
        sorted(path.name for path in self.backup_dir.iterdir()),
        ["spotify_backup_current.json"],
    )

# spotify-backup/test_auth_helper.py
def test_default_cache_path_lives_inside_repo_data_dir(self):
    module = load_auth_helper_module(self)
    expected = module.PROJECT_DIR / "data" / ".cache"
    self.assertEqual(module.default_cache_path(), expected)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest spotify-backup/test_backup.py spotify-backup/test_auth_helper.py -v`
Expected: FAIL because `save_backup()` still writes timestamped files and `auth_helper.py` does not expose repo-relative cache path helpers.

- [ ] **Step 3: Write the minimal implementation**

```python
# spotify-backup/backup.py
CURRENT_BACKUP_NAME = "spotify_backup_current.json"

def save_backup(data):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    final_path = os.path.join(BACKUP_DIR, CURRENT_BACKUP_NAME)
    temp_path = f"{final_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, final_path)
    return final_path

# spotify-backup/auth_helper.py
PROJECT_DIR = Path(__file__).resolve().parent

def default_cache_path() -> Path:
    return PROJECT_DIR / "data" / ".cache"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest spotify-backup/test_backup.py spotify-backup/test_auth_helper.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add spotify-backup/test_backup.py spotify-backup/test_auth_helper.py spotify-backup/backup.py spotify-backup/auth_helper.py
git commit -m "test: cover spotify backup mirror output and auth cache path"
```

### Task 2: Add failing tests for auth error handling and implement clear headless behavior

**Files:**
- Modify: `spotify-backup/test_backup.py`
- Modify: `spotify-backup/backup.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_main_returns_failure_when_oauth_cache_is_missing(self):
    module = load_backup_module(self)
    module.CLIENT_ID = "client"
    module.CLIENT_SECRET = "secret"
    module.CACHE_PATH = str(self.base / ".cache")

    with self.assertLogs(module.logger, level="ERROR") as captured:
        result = module.main()

    self.assertEqual(result, 1)
    self.assertIn("OAuth cache", "\n".join(captured.output))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest spotify-backup/test_backup.py -v`
Expected: FAIL because `main()` currently returns `None` and does not explicitly reject missing cache before trying interactive auth.

- [ ] **Step 3: Write the minimal implementation**

```python
def main():
    if not os.path.exists(CACHE_PATH):
        logger.error("OAuth cache not found at %s. Run spotify-backup/auth_helper.py externally first.", CACHE_PATH)
        return 1
    ...
    except Exception as exc:
        logger.error("Backup failed: %s", exc)
        return 1
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest spotify-backup/test_backup.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add spotify-backup/test_backup.py spotify-backup/backup.py
git commit -m "feat: fail spotify backup clearly when auth cache is missing"
```

### Task 3: Add failing tests for schedule generation and implement 4-hour startup behavior

**Files:**
- Create: `spotify-backup/test_entrypoint.py`
- Modify: `spotify-backup/entrypoint.sh`

- [ ] **Step 1: Write the failing test**

```python
def test_entrypoint_schedules_four_hour_interval(self):
    completed = subprocess.run(
        ["bash", "spotify-backup/entrypoint.sh"],
        cwd=REPO_ROOT,
        env={**minimal_env, "BACKUP_INTERVAL_MINUTES": "240", "PATH": fake_path},
        capture_output=True,
        text=True,
    )
    self.assertIn("Scheduling backup with expression: 0 */4 * * *", completed.stdout)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest spotify-backup/test_entrypoint.py -v`
Expected: FAIL if schedule generation or logging does not deterministically emit the expected 4-hour cron line.

- [ ] **Step 3: Write the minimal implementation**

```bash
if [ "$((BACKUP_INTERVAL_MINUTES % 60))" -eq 0 ]; then
    HOURS=$((BACKUP_INTERVAL_MINUTES / 60))
    if [ "$HOURS" -le 23 ]; then
        CRON_EXPRESSION="0 */${HOURS} * * *"
    else
        CRON_EXPRESSION="0 0 */$((BACKUP_INTERVAL_MINUTES / 1440)) * *"
    fi
fi
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest spotify-backup/test_entrypoint.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add spotify-backup/test_entrypoint.py spotify-backup/entrypoint.sh
git commit -m "fix: schedule spotify backup every four hours"
```

### Task 4: Wire the compose service and verify service-only runtime

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Write the failing config expectation**

```text
Service name: spotify-backup
Expected mounts: ./spotify-backup/data:/data and ./spotify-backup/logs:/logs
Expected env: BACKUP_INTERVAL_MINUTES=240, LOG_DIR=/logs, BACKUP_DIR=/data/backup, CACHE_PATH=/data/.cache
```

- [ ] **Step 2: Run config check to verify the service is absent or incomplete**

Run: `docker compose config`
Expected: `spotify-backup` missing or missing one or more required mounts/env values.

- [ ] **Step 3: Write the minimal implementation**

```yaml
  spotify-backup:
    build:
      context: ./spotify-backup
      args:
        TARGETARCH: ${TARGETARCH:-amd64}
    image: caldav-sync/spotify-backup:local
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./spotify-backup/data:/data
      - ./spotify-backup/logs:/logs
    environment:
      - BACKUP_INTERVAL_MINUTES=240
      - BACKUP_DIR=/data/backup
      - CACHE_PATH=/data/.cache
      - LOG_DIR=/logs
```

- [ ] **Step 4: Run config check to verify it passes**

Run: `docker compose config`
Expected: PASS with a valid `spotify-backup` service definition.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: add spotify-backup compose service"
```

### Task 5: Validate spotify-backup only

**Files:**
- No code changes required if all prior tasks pass

- [ ] **Step 1: Run focused unit tests**

Run: `python3 -m unittest spotify-backup/test_backup.py spotify-backup/test_auth_helper.py spotify-backup/test_entrypoint.py -v`
Expected: PASS

- [ ] **Step 2: Run compose config validation**

Run: `docker compose config`
Expected: PASS

- [ ] **Step 3: Run service-only build and startup validation**

Run: `docker compose up --build -d spotify-backup`
Expected: image builds and only `spotify-backup` starts

- [ ] **Step 4: Inspect spotify-backup logs**

Run: `docker compose logs --tail=100 spotify-backup`
Expected: startup log line, initial backup attempt, scheduler line with `0 */4 * * *`

- [ ] **Step 5: Inspect output state**

Run: `find spotify-backup/data -maxdepth 2 -type f | sort`
Expected: only current-state backup file, OAuth cache, and any expected runtime artifacts

- [ ] **Step 6: Commit final implementation batch**

```bash
git add docker-compose.yml spotify-backup
git commit -m "feat: run spotify-backup as a headless mirror service"
```
