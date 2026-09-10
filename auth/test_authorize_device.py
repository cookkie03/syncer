#!/usr/bin/env python3
"""Regression tests for headless Google auth helper."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "auth" / "authorize-device.py"


def load_module():
    spec = importlib.util.spec_from_file_location("authorize_device", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AuthorizeDeviceImportTests(unittest.TestCase):
    def test_script_imports_with_stdlib_only(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                (
                    "import importlib.util; "
                    f"spec = importlib.util.spec_from_file_location('authorize_device', r'{SCRIPT}'); "
                    "module = importlib.util.module_from_spec(spec); "
                    "spec.loader.exec_module(module)"
                ),
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_pick_ca_bundle_uses_known_system_bundle(self) -> None:
        module = load_module()
        selected = module.pick_ca_bundle(
            candidates=[
                Path("/tmp/does-not-exist.pem"),
                Path("/etc/ssl/certs/ca-certificates.crt"),
            ]
        )
        self.assertEqual(selected, Path("/etc/ssl/certs/ca-certificates.crt"))

    def test_choose_client_credentials_requires_device_client(self) -> None:
        module = load_module()
        with self.assertRaises(RuntimeError):
            module.choose_client_credentials(
                {
                    "GOOGLE_CLIENT_ID": "desktop-id",
                    "GOOGLE_CLIENT_SECRET": "desktop-secret",
                }
            )

    def test_token_poll_uses_google_device_parameters(self) -> None:
        module = load_module()
        payload = module.build_token_request_data(
            client_id="cid",
            client_secret="secret",
            device_code="device-code",
        )
        self.assertEqual(payload["code"], "device-code")
        self.assertEqual(payload["grant_type"], "http://oauth.net/grant_type/device/1.0")
        self.assertNotIn("device_code", payload)

    def test_supported_scopes_exclude_gmail(self) -> None:
        module = load_module()
        services = [name for name, _, _ in module.scopes_to_tokens()]
        self.assertEqual(services, ["Google Calendar", "Google Contacts"])


if __name__ == "__main__":
    unittest.main()
