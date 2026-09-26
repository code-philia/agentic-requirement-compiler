from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .process_utils import resolve_executable, run_command


class GitStageError(RuntimeError):
    pass


class ProjectGitHistory:
    """Commit only compiler-owned paths in the generated application."""

    def __init__(self, output_root: Path) -> None:
        self.root = output_root.expanduser().resolve()
        self.executable = resolve_executable("git", os.environ)

    def initialize(self) -> None:
        if (self.root / ".git").exists():
            raise GitStageError("Generated project already contains a Git repository.")
        self._run(["init"])

    def commit(self, stage: str, paths: list[str]) -> None:
        if not paths:
            raise GitStageError(f"No compiler-owned paths for {stage}.")
        self._run(["add", "-A", "--", *sorted(set(paths))])
        self._run([
            "-c", "user.name=ARC Compiler",
            "-c", "user.email=arc@local.invalid",
            "commit", "--allow-empty", "-m", f"ARC: {stage}",
        ])

    def _run(self, args: list[str]) -> None:
        if self.executable is None:
            raise GitStageError("git is required to compile the generated project.")
        try:
            result = run_command(
                [self.executable, *args], cwd=self.root,
                environment=os.environ, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitStageError(f"git {args[0]} failed: {exc}") from exc
        if result.returncode != 0:
            raise GitStageError(
                f"git {args[0]} failed: {result.stderr or result.stdout}"
            )
