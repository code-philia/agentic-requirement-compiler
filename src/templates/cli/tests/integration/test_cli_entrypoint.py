from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


class CliEntrypointIntegrationTests(unittest.TestCase):
    def test_python_module_entrypoint_prints_name(self) -> None:
        workspace = Path(__file__).resolve().parents[2]
        completed = subprocess.run(
            [sys.executable, "-m", "app", "--name", "ARC"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "Hello, ARC!")
        self.assertEqual(completed.stderr.strip(), "")


if __name__ == "__main__":
    unittest.main()
