from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

from .base import AppTypeHandler


def _normalize_cli_test_path(file_path: str) -> str:
    return str(file_path or "").strip().replace("\\", "/").lstrip("./")


def _is_valid_cli_test_filename(file_path: str) -> bool:
    name = Path(file_path).name
    return bool(re.match(r"^(test_.*|.*_test)\.py$", name))


def _cli_test_module(file_path: str) -> str:
    normalized = _normalize_cli_test_path(file_path)
    if normalized.lower().endswith(".py"):
        normalized = normalized[:-3]
    return normalized.replace("/", ".")


async def _run_python_command(
    args: list[str],
    *,
    cwd: str,
    timeout: float = 120.0,
) -> str:
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        result = f"Exit Code: {process.returncode}\n"
        if output:
            result += f"STDOUT:\n{output}\n"
        if error:
            result += f"STDERR:\n{error}\n"
        return result
    except asyncio.TimeoutError:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        return f"Exit Code: 124\nSTDERR:\nCommand timed out after {timeout} seconds.\n"
    except Exception as exc:
        return f"Exit Code: 1\nSTDERR:\nExecution failed: {str(exc)}\n"


class CliAppType(AppTypeHandler):
    name = "cli"

    @classmethod
    def prerequisite_commands(cls) -> list[str]:
        return ["python"]

    @classmethod
    def runtime_contract_lines(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> list[str]:
        del web_port, android_package
        return [
            "For CLI apps, the runtime is the terminal: execute the project from the workspace root with `python -m app`.",
            "The user-visible contract is command arguments, stdout/stderr text, exit codes, and owned filesystem side effects.",
            "Do not assume a browser, background server, or mobile lifecycle unless the requirement explicitly introduces one through owned subprocess behavior.",
        ]

    @classmethod
    def project_structure_lines(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> list[str]:
        del web_port, android_package
        return [
            "- CLI structure rules:",
            "  - Runtime entrypoint: `app/__main__.py` delegates to `app/main.py`.",
            "  - Command parsing and terminal I/O should live under `app/`.",
            "  - Business logic, persistence, and helper modules should stay under `app/` rather than ad-hoc top-level scripts.",
            "  - Unit tests: `tests/unit/...`",
            "  - Integration tests: `tests/integration/...`",
            "  - E2E tests: `tests/e2e/...`",
            "  - Prefer command entrypoints, owner modules, and direct dependencies before broader search.",
        ]

    @classmethod
    def test_harness_lines(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> list[str]:
        del web_port, android_package
        return [
            "Test manifest `type` must be one of `Unit`, `Integration`, or `E2E`.",
            "Unit tests: place under `tests/unit/...` and use a `test_*.py` or `*_test.py` filename.",
            "Integration tests: place under `tests/integration/...` and use a `test_*.py` or `*_test.py` filename.",
            "E2E tests: place under `tests/e2e/...` and use a `test_*.py` or `*_test.py` filename.",
            "CLI tests should verify `python -m app ...` behavior through exit codes, stdout/stderr, and owned side effects when command execution is part of the requirement.",
        ]

    @classmethod
    def build_stack_block(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> str:
        del web_port, android_package
        return "\n".join(
            [
                "* **Platform** : Terminal CLI application",
                "* **Language** : Python 3.11+ (stdlib-first template)",
                "* **Runtime Entry** : `python -m app` via `app/__main__.py`",
                "* **Argument Parsing** : `argparse`",
                "* **Persistence** : stdlib files / JSON / SQLite when the requirement owns persistence",
                "* **Testing** : Python `unittest` organized under `tests/unit/`, `tests/integration/`, and `tests/e2e/`",
                "* **Process Model** : single-process command execution by default; subprocess behavior should be requirement-owned and explicit",
            ]
        )

    @classmethod
    def default_stack_summary(cls) -> str:
        return "runtime=python-cli, test_runner=unittest"

    @classmethod
    def parse_stack_summary(cls, metadata_content: str) -> str:
        del metadata_content
        return cls.default_stack_summary()

    def validate_test_path(self, test_type: str, file_path: str) -> str | None:
        normalized_type = (test_type or "").strip().lower()
        normalized_path = _normalize_cli_test_path(file_path)
        if normalized_type not in {"unit", "integration", "e2e"}:
            return "CLI test `type` must be one of `Unit`, `Integration`, or `E2E`."
        expected_prefix = f"tests/{normalized_type}/"
        if not normalized_path.startswith(expected_prefix):
            return (
                f"CLI {test_type} tests must live under `{expected_prefix}...`. "
                f"Received: {file_path}"
            )
        if not _is_valid_cli_test_filename(normalized_path):
            return (
                "CLI test files must use a `test_*.py` or `*_test.py` filename. "
                f"Received: {file_path}"
            )
        return None

    async def run_test_file(self, test_type: str, file_path: str) -> str:
        await self._log("System", f"System test execution ({test_type}): {file_path}")
        validation_error = self.validate_test_path(test_type, file_path)
        if validation_error:
            return f"Exit Code: 1\nSTDERR:\n{validation_error}\n"
        module_name = _cli_test_module(file_path)
        return await _run_python_command(
            [sys.executable, "-m", "unittest", "-v", module_name],
            cwd=self.workspace_path,
        )

    async def run_test_group(self, test_type: str, file_paths: list[str]) -> str:
        if not file_paths:
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                f"No test files were configured for the current {test_type} batch.\n"
            )
        invalid_paths = [path for path in file_paths if self.validate_test_path(test_type, path)]
        if invalid_paths:
            error_lines = ["Exit Code: 1", "STDERR:"]
            for path in invalid_paths:
                error_lines.append(self.validate_test_path(test_type, path) or "")
            return "\n".join(error_lines) + "\n"
        await self._log("System", f"System test execution ({test_type}) batch: {', '.join(file_paths)}")
        modules = [_cli_test_module(path) for path in file_paths]
        return await _run_python_command(
            [sys.executable, "-m", "unittest", "-v", *modules],
            cwd=self.workspace_path,
        )

    async def run_build(self) -> str:
        targets = [path for path in ("app", "tests") if os.path.exists(os.path.join(self.workspace_path, path))]
        if not targets:
            return "Exit Code: 1\nSTDERR:\nNo CLI source directories were found for build verification.\n"
        return await _run_python_command(
            [sys.executable, "-m", "compileall", *targets],
            cwd=self.workspace_path,
        )
