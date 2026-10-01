"""Local launcher keeps tokens out of command arguments and excludes other service secrets."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RunTodoistTests(unittest.TestCase):
    def test_launcher_loads_only_todoist_values_and_propagates_exit_code(self):
        spec = importlib.util.spec_from_file_location("run_todoist", Path(__file__).with_name("run-todoist.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "setup").mkdir()
            (root / ".env").write_text("TODOIST_API_TOKEN=private-test-token\nCALDAV_PASSWORD=unrelated-secret\n")
            module.__file__ = str(root / "setup" / "run-todoist.py")
            with patch.object(module.sys, "argv", ["run-todoist.py", "backup"]), \
                 patch.object(module.os, "environ", {}), \
                 patch.object(module.subprocess, "call", return_value=7) as call:
                self.assertEqual(module.main(), 7)
            args, kwargs = call.call_args
            self.assertNotIn("private-test-token", " ".join(args[0]))
            self.assertEqual(kwargs["env"]["TODOIST_API_TOKEN"], "private-test-token")
            self.assertNotIn("CALDAV_PASSWORD", kwargs["env"])


if __name__ == "__main__":
    unittest.main()
