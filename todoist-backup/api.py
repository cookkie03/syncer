"""Read-only Todoist transport. No mutation endpoints or Sync commands allowed."""

from __future__ import annotations

import email.utils
import ipaddress
import json
import os
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import requests


BASE = "https://api.todoist.com/api/v1/"


class APIError(RuntimeError):
    def __init__(self, status=0, tag="request_failed"):
        self.status, self.tag = status, tag
        super().__init__(f"Todoist request failed: HTTP {status} ({tag})")


class AuthenticationError(APIError):
    pass


class BudgetReached(RuntimeError):
    pass


def retry_delay(value, attempt):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = (email.utils.parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            delay = 2 ** attempt
    return max(0, delay)


def public_url(url):
    """Download only public HTTPS locations; never forward the API token."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise APIError(tag="invalid_download_url")
    try:
        port = parsed.port
    except ValueError:
        raise APIError(tag="invalid_download_port") from None
    if port not in (None, 443):
        raise APIError(tag="invalid_download_port")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise APIError(tag="nonpublic_download_url")
    except OSError:
        raise APIError(tag="download_dns_failed") from None


class Client:
    def __init__(self, token, *, session=None, sleep=time.sleep, max_requests=600, max_seconds=1800):
        self.token = token
        if max_requests <= 0 or max_seconds <= 0:
            raise ValueError("Request and time budgets must be positive")
        self.session = session or requests.Session()
        self.sleep = sleep
        self.remaining = max_requests
        self.deadline = time.monotonic() + max_seconds

    def _budget(self):
        if self.remaining <= 0 or time.monotonic() >= self.deadline:
            raise BudgetReached("Run budget reached; resume on next run")
        self.remaining -= 1

    def _request(self, method, url, *, authenticated=True, **kwargs):
        headers = {"Authorization": f"Bearer {self.token}"} if authenticated else {}
        for attempt in range(4):
            self._budget()
            try:
                response = self.session.request(method, url, headers=headers, timeout=(10, 60),
                                                allow_redirects=False, **kwargs)
            except requests.RequestException:
                if attempt == 3:
                    raise APIError(tag="network_error") from None
                self.sleep(2 ** attempt)
                continue
            if response.status_code == 401 and authenticated:
                response.close()
                raise AuthenticationError(401, "invalid_credentials")
            if response.status_code == 429 or response.status_code >= 500:
                delay = retry_delay(response.headers.get("Retry-After"), attempt)
                status = response.status_code
                response.close()
                # Never retry before Retry-After; a long delay defers to the next run.
                if attempt == 3 or delay > 60 or time.monotonic() + delay >= self.deadline:
                    raise APIError(status, "rate_limited" if status == 429 else "server_error")
                self.sleep(delay)
                continue
            return response
        raise APIError()

    def read(self, path, params=None, *, sync_token=None):
        if path.startswith(("/", "http")) or ".." in path or "?" in path:
            raise ValueError("Expected relative API path")
        try:
            if sync_token is not None:
                if path != "sync" or params is not None:
                    raise ValueError("Sync supports only resource reads")
                response = self._request("POST", BASE + path, data={
                    "sync_token": sync_token, "resource_types": json.dumps(["all"]),
                })
            else:
                response = self._request("GET", BASE + path, params=params or {})
        except AuthenticationError:
            if path != "backups":
                raise
            # MFA can reject this endpoint despite a valid account token.
            # A failed recheck still propagates the fatal credentials error.
            self.read("user")
            raise APIError(401, "backup_mfa_required") from None
        try:
            if response.status_code != 200:
                raise APIError(response.status_code, "access_or_endpoint_unavailable")
            try:
                data = response.json()
            except ValueError:
                raise APIError(200, "invalid_json") from None
            if isinstance(data, dict) and (data.get("error") or data.get("error_tag")):
                raise APIError(200, "api_error_response")
            return data
        finally:
            response.close()

    def pages(self, path, params=None):
        params = dict(params or {})
        params.setdefault("limit", 100)
        seen = set()
        while True:
            data = self.read(path, params)
            yield data
            if isinstance(data, list):
                return
            if not isinstance(data, dict):
                raise APIError(200, "invalid_page")
            cursor = data.get("next_cursor")
            if not cursor or data.get("has_more") is False:
                return
            if cursor in seen:
                raise APIError(200, "repeated_cursor")
            seen.add(cursor)
            params["cursor"] = cursor

    def download(self, url, destination, *, archive=False):
        # API backup download redirects to a signed CDN URL. Token stays at api.todoist.com.
        if archive:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc != "api.todoist.com" or parsed.path != "/api/v1/backups/download":
                raise APIError(tag="invalid_archive_url")
        started = time.monotonic()
        for redirect in range(6):
            authenticated = archive and redirect == 0
            if not authenticated:
                public_url(url)
            try:
                response = self._request("GET", url, authenticated=authenticated, stream=True)
            except AuthenticationError:
                if not authenticated:
                    raise
                self.read("user")
                raise APIError(401, "backup_mfa_required") from None
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise APIError(tag="missing_redirect")
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    raise APIError(response.status_code, "download_unavailable")
                total = 0
                with open(destination, "wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        if time.monotonic() - started > 300 or time.monotonic() >= self.deadline:
                            raise APIError(tag="download_timeout")
                        output.write(chunk)
                        total += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                length = response.headers.get("Content-Length")
                if length and not response.headers.get("Content-Encoding"):
                    try:
                        expected = int(length)
                    except ValueError:
                        raise APIError(tag="invalid_content_length") from None
                    if total != expected:
                        raise APIError(tag="truncated_download")
                return total
            except requests.RequestException:
                raise APIError(tag="download_interrupted") from None
            finally:
                response.close()
        raise APIError(tag="too_many_redirects")
