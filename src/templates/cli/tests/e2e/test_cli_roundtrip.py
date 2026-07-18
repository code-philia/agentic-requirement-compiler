from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


class CliRoundtripE2ETests(unittest.TestCase):
    def test_uppercase_flag_changes_user_visible_output(self) -> None:
        workspace = Path(__file__).resolve().parents[2]
        completed = subprocess.run(
            [sys.executable, "-m", "app", "--name", "arc", "--uppercase"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "HELLO, ARC!")
        self.assertEqual(completed.stderr.strip(), "")


if __name__ == "__main__":
    unittest.main()
