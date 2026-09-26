from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .process_utils import resolve_executable, run_command


@dataclass(slots=True)
class ProjectBuildResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


class ProjectBuilder:
    """Run the generated workspace build as a synchronous acceptance gate."""

    def __init__(
        self,
        output_root: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.output_root = output_root.expanduser().resolve()
        self.environment = dict(os.environ if environment is None else environment)

    def build(self) -> ProjectBuildResult:
        package_path = self.output_root / "package.json"
        if not package_path.is_file():
            return ProjectBuildResult(
                ok=False,
                errors=[
                    "ARC3501 PROJECT_BUILD_FAILED: "
                    f"Generated workspace package.json does not exist: {package_path}"
                ],
            )

        executable = resolve_executable("npm", self.environment)
        if executable is None:
            return ProjectBuildResult(
                ok=False,
                errors=[
                    "ARC3501 PROJECT_BUILD_FAILED: Required command is unavailable: npm"
                ],
            )

        command = [executable, "run", "build"]
        try:
            completed = run_command(
                command,
                cwd=self.output_root,
                environment=self.environment,
                timeout=900,
            )
        except FileNotFoundError:
            return ProjectBuildResult(
                ok=False,
                errors=[
                    "ARC3501 PROJECT_BUILD_FAILED: Required command is unavailable: npm"
                ],
            )
        except subprocess.TimeoutExpired as exc:
            detail = self._command_output(exc.stdout, exc.stderr)
            message = "ARC3502 PROJECT_BUILD_TIMEOUT: npm run build exceeded 900 seconds."
            if detail:
                message = f"{message}\n{detail}"
            return ProjectBuildResult(ok=False, errors=[message])
        except OSError as exc:
            return ProjectBuildResult(
                ok=False,
                errors=[f"ARC3501 PROJECT_BUILD_FAILED: Cannot run npm build: {exc}"],
            )

        if completed.returncode != 0:
            detail = self._command_output(completed.stdout, completed.stderr)
            message = (
                "ARC3503 PROJECT_BUILD_FAILED: "
                f"npm run build exited with {completed.returncode}."
            )
            if detail:
                message = f"{message}\n{detail}"
            return ProjectBuildResult(ok=False, errors=[message])

        return ProjectBuildResult(ok=True)

    @staticmethod
    def _command_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
        def normalize(value: str | bytes | None) -> str:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return str(value or "")

        detail = "\n".join(part for part in (normalize(stdout), normalize(stderr)) if part)
        return detail
