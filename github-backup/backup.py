#!/usr/bin/env python3
"""Mirror owned and starred GitHub repositories to local bare repositories."""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/backup"))
STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
LOG_DIR = Path(os.getenv("LOG_DIR", "/logs"))
MIRRORS_DIR = BACKUP_DIR / "mirrors"
STATUS_PATH = STATE_DIR / "status.json"
API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com")
SCHEDULE = os.getenv("GITHUB_BACKUP_SCHEDULE", "@daily")
MAX_DELETION_RATIO = float(os.getenv("GITHUB_MAX_DELETION_RATIO", "0.5"))
REMOVE_STALE = os.getenv("GITHUB_REMOVE_STALE", "0") == "1"

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "github-backup.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def github_request(path: str, token: str, page: int = 1) -> list[dict] | dict:
    """Fetch one GitHub API page without exposing the token in logs."""
    url = f"{API_URL.rstrip('/')}{path}"
    separator = "&" if "?" in url else "?"
    request = Request(
        f"{url}{separator}per_page=100&page={page}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "syncer-github-backup",
        },
    )
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def paginate(path: str, token: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        result = github_request(path, token, page)
        if not isinstance(result, list):
            raise RuntimeError(f"GitHub API returned an unexpected response for {path}")
        items.extend(result)
        if len(result) < 100:
            return items
        page += 1


def discover_repositories(token: str) -> tuple[dict[str, dict], str]:
    """Return canonical repositories and the authenticated account login."""
    user = github_request("/user", token)
    if not isinstance(user, dict) or not user.get("login"):
        raise RuntimeError("GitHub API did not return the authenticated user")
    login = str(user["login"])
    owned = paginate("/user/repos?affiliation=owner&sort=full_name", token)
    starred = paginate("/user/starred?sort=created", token)
    repositories: dict[str, dict] = {}
    for repository in owned:
        repositories[repository["full_name"]] = {
            "full_name": repository["full_name"],
            "clone_url": repository["clone_url"],
            "owned": True,
            "starred": False,
        }
    for repository in starred:
        full_name = repository["full_name"]
        record = repositories.setdefault(
            full_name,
            {
                "full_name": full_name,
                "clone_url": repository["clone_url"],
                "owned": False,
                "starred": False,
            },
        )
        record["starred"] = True
    return repositories, login


def _git_environment(token: str) -> dict[str, str]:
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
        }
    )
    return environment


def mirror_repository(repository: dict, token: str) -> None:
    """Create a complete mirror in staging, then replace the old mirror."""
    full_name = repository["full_name"]
    destination = MIRRORS_DIR / f"{full_name}.git"
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=".mirror-", dir=BACKUP_DIR))
    staging = staging_root / f"{full_name}.git"
    try:
        subprocess.run(
            ["git", "clone", "--mirror", repository["clone_url"], str(staging)],
            check=True,
            capture_output=True,
            text=True,
            env=_git_environment(token),
        )
        old_destination = destination.with_name(destination.name + ".previous")
        if old_destination.exists():
            shutil.rmtree(old_destination)
        if destination.exists():
            destination.rename(old_destination)
        try:
            staging.rename(destination)
        except Exception:
            if old_destination.exists() and not destination.exists():
                old_destination.rename(destination)
            raise
        if old_destination.exists():
            shutil.rmtree(old_destination)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "git clone failed").strip()
        raise RuntimeError(f"mirror update failed for {full_name}: {detail}") from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def load_status() -> dict:
    if not STATUS_PATH.exists():
        return {}
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Ignoring unreadable status file: %s", STATUS_PATH)
        return {}


def save_status(status: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, STATUS_PATH)


def cleanup_stale(stale: set[str], known_mirrors: set[str], status: dict) -> None:
    """Remove stale mirrors only when explicitly enabled and below the safety limit."""
    if not REMOVE_STALE or not stale:
        return
    ratio = len(stale) / len(known_mirrors) if known_mirrors else 0
    if ratio > MAX_DELETION_RATIO:
        logger.error(
            "BLOCKED stale mirror cleanup: %d of %d repositories (%.1f%%) exceeds %.1f%%; "
            "all local data was preserved",
            len(stale),
            len(known_mirrors),
            ratio * 100,
            MAX_DELETION_RATIO * 100,
        )
        status["cleanup"] = {
            "state": "blocked",
            "planned_removals": len(stale),
            "existing_mirrors": len(known_mirrors),
            "ratio": ratio,
        }
        return
    for full_name in stale:
        shutil.rmtree(MIRRORS_DIR / f"{full_name}.git")
    status["cleanup"] = {
        "state": "complete",
        "planned_removals": len(stale),
        "existing_mirrors": len(known_mirrors),
        "ratio": ratio,
    }


def run_backup() -> int:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        logger.error("GITHUB_TOKEN is required")
        return 1
    started = datetime.now(timezone.utc).isoformat()
    status = load_status()
    try:
        repositories, login = discover_repositories(token)
        MIRRORS_DIR.mkdir(parents=True, exist_ok=True)
        known_mirrors = {
            path.relative_to(MIRRORS_DIR).as_posix()[:-4]
            for path in MIRRORS_DIR.glob("**/*.git")
            if path.is_dir()
        }
        current_names = set(repositories)
        stale = known_mirrors - current_names
        for repository in repositories.values():
            mirror_repository(repository, token)
        status.update(
            {
                "state": "complete",
                "account": login,
                "started_at": started,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "repository_count": len(repositories),
                "stale_repositories": sorted(stale),
                "schedule": SCHEDULE,
            }
        )
        cleanup_stale(stale, known_mirrors, status)
        save_status(status)
        logger.info("GitHub mirror complete: %d repositories, %d stale", len(repositories), len(stale))
        return 0
    except Exception as exc:
        status.update({"state": "failed", "started_at": started, "error": str(exc)})
        save_status(status)
        logger.exception("GitHub mirror failed")
        return 1


def health() -> int:
    status = load_status()
    if status.get("state") != "complete":
        logger.error("GitHub mirror is not healthy: %s", status.get("state", "never completed"))
        return 1
    if status.get("cleanup", {}).get("state") == "blocked":
        logger.error("GitHub mirror cleanup is blocked by the deletion guardrail")
        return 1
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(health() if "--health" in sys.argv else run_backup())
