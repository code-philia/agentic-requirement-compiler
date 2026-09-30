from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from core.logging import SynchronousLog

from .process_utils import (
    process_group_kwargs,
    resolve_executable,
    terminate_process_tree,
)
from .test_generation import TEST_ENVIRONMENT_READY, TEST_LAYERS, TESTS_FROZEN


@dataclass(frozen=True, slots=True)
class TestSelection:
    """Select one requirement's frozen tests without repository discovery."""

    requirement_id: str
    layers: tuple[str, ...] = ()
    stop_on_failure: bool = True


@dataclass(slots=True)
class TestCommandResult:
    phase: str
    layer: str | None
    command: list[str]
    test_files: list[str]
    status: str
    returncode: int | None
    duration_ms: int
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


@dataclass(slots=True)
class TestRunResult:
    requirement_id: str
    status: str
    selected_layers: list[str]
    selected_files: list[str]
    commands: list[TestCommandResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "PASSED" and not self.errors

class TestRunner:
    """Run exact, integrity-checked tests from the frozen manifest."""

    def __init__(
        self,
        output_root: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.output_root = output_root.expanduser().resolve()
        self.environment = dict(os.environ if environment is None else environment)
        self.environment.setdefault("CI", "1")
        self._log = SynchronousLog("TestRunner", workspace_root=self.output_root)
        self._timeouts = {
            "TYPECHECK": _bounded_float(
                self.environment,
                "ARC_TDD_TYPECHECK_TIMEOUT_SECONDS",
                300.0,
                3.0,
                900.0,
            ),
            "UNIT": _bounded_float(
                self.environment,
                "ARC_TDD_UNIT_TIMEOUT_SECONDS",
                120.0,
                3.0,
                900.0,
            ),
            "INTEGRATION": _bounded_float(
                self.environment,
                "ARC_TDD_INTEGRATION_TIMEOUT_SECONDS",
                180.0,
                3.0,
                900.0,
            ),
            "E2E": _bounded_float(
                self.environment,
                "ARC_TDD_E2E_TIMEOUT_SECONDS",
                180.0,
                3.0,
                1800.0,
            ),
        }

    def run(
        self,
        selection: TestSelection,
        *,
        test_manifest: dict[str, Any] | None = None,
    ) -> TestRunResult:
        """Run only the selected requirement files, stopping at the first failed layer."""

        started = time.perf_counter()
        requirement_id = str(selection.requirement_id).strip()
        manifest, manifest_errors = self._load_manifest(
            test_manifest,
            self.output_root / ".arc" / "tests" / "test_manifest.json",
            "ARC4501 TEST_MANIFEST_INVALID",
        )
        project, environment_errors = self._load_manifest(
            None,
            self.output_root / ".arc" / "project" / "project-manifest.json",
            "ARC4502 TEST_ENVIRONMENT_INVALID",
        )
        environment = project.get("testEnvironment")
        if not isinstance(environment, dict):
            environment = {}
        errors = [*manifest_errors, *environment_errors]
        if not requirement_id:
            errors.append("ARC4503 TEST_SELECTION_INVALID: requirement_id is required.")
        if manifest.get("status") != TESTS_FROZEN:
            errors.append(
                "ARC4501 TEST_MANIFEST_INVALID: Test Runner requires TESTS_FROZEN."
            )
        if environment.get("status") != TEST_ENVIRONMENT_READY:
            errors.append(
                "ARC4502 TEST_ENVIRONMENT_INVALID: Test Runner requires "
                "TEST_ENVIRONMENT_READY."
            )
        layers, layer_errors = _normalize_layers(selection.layers)
        errors.extend(layer_errors)
        file_rows = [
            row for row in manifest.get("files", [])
            if isinstance(row, dict) and row.get("requirement_id") == requirement_id
        ]
        available_layers = [
            layer for layer in TEST_LAYERS
            if any(row.get("layer") == layer for row in file_rows)
        ]
        selected_layers = layers or available_layers
        if not selected_layers or any(layer not in available_layers for layer in selected_layers):
            errors.append(f"ARC4503 TEST_SELECTION_INVALID: no frozen files for {requirement_id}: {selected_layers}.")
        selected_rows = [
            row for layer in selected_layers for row in file_rows if row.get("layer") == layer
        ]
        if any(sum(row.get("layer") == layer for row in selected_rows) != 1 for layer in selected_layers):
            errors.append("ARC4501 TEST_MANIFEST_INVALID: expected one frozen file per selected layer.")
        selected_files: list[str] = []
        for row in selected_rows:
            file_error = self._validate_frozen_file(row)
            if file_error:
                errors.append(file_error)
                continue
            selected_files.append(str(row["test_file"]))

        result = TestRunResult(
            requirement_id=requirement_id,
            status="INVALID_SELECTION" if errors else "RUNNING",
            selected_layers=selected_layers,
            selected_files=selected_files,
            errors=list(dict.fromkeys(errors)),
        )
        if errors:
            result.duration_ms = round((time.perf_counter() - started) * 1000)
            return result

        for layer in TEST_LAYERS:
            if layer not in selected_layers:
                continue
            layer_files = [
                str(row["test_file"])
                for row in selected_rows
                if str(row.get("layer", "")).upper() == layer
            ]
            command = _execution_command(layer, layer_files)
            command_result = self._execute(
                phase="EXECUTION",
                layer=layer,
                command=command,
                test_files=layer_files,
                timeout=(
                    self._timeouts["TYPECHECK"] + self._timeouts[layer] + 20
                    if layer == "INTEGRATION" else self._timeouts[layer]
                ),
            )
            result.commands.append(command_result)
            if command_result.status != "PASSED" and selection.stop_on_failure:
                break
        return self._finish(result, started)

    def run_changed_typechecks(self, changed_files: list[str]) -> list[TestCommandResult]:
        """Check only workspaces touched by a source-only patch."""
        paths = {str(path).replace("\\", "/").lstrip("./") for path in changed_files}
        workspaces = [
            name for name in ("frontend", "backend", "tests")
            if any(path.startswith(f"{name}/") for path in paths)
        ]
        return [self.run_workspace_typecheck(workspace=name) for name in workspaces]

    def run_workspace_typecheck(self, workspace: str | None = None) -> TestCommandResult:
        """Type-check the whole project or one generated workspace."""
        if workspace is not None and workspace not in {"frontend", "backend", "tests"}:
            raise ValueError(f"Unsupported typecheck workspace: {workspace}")
        command = ["npm", "run", "typecheck"]
        if workspace is not None:
            command.extend(["-w", f"@arc/{workspace}"])
        return self._execute(
            phase="TYPECHECK",
            layer=None,
            command=command,
            test_files=[],
            timeout=self._timeouts["TYPECHECK"],
        )

    def _validate_frozen_file(self, row: dict[str, Any]) -> str | None:
        relative = str(row.get("test_file", "")).replace("\\", "/").strip().strip("/")
        layer = str(row.get("layer", "")).upper()
        path = PurePosixPath(relative)
        expected_root = f"tests/{layer.lower()}/"
        if (
            layer not in TEST_LAYERS
            or not relative.startswith(expected_root)
            or not relative.endswith(".spec.ts")
            or path.is_absolute()
            or ".." in path.parts
        ):
            return f"ARC4504 TEST_FILE_INVALID: invalid frozen test row for {relative!r}."
        target = (self.output_root / Path(relative)).resolve()
        if self.output_root not in target.parents or not target.is_file():
            return f"ARC4504 TEST_FILE_INVALID: frozen test does not exist: {relative}."
        try:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError as exc:
            return f"ARC4504 TEST_FILE_INVALID: cannot read {relative}: {exc}"
        if digest != str(row.get("content_sha256", "")):
            return f"ARC4505 TEST_INTEGRITY_FAILED: frozen test changed: {relative}."
        return None

    def _execute(
        self,
        *,
        phase: str,
        layer: str | None,
        command: list[str],
        test_files: list[str],
        timeout: float,
    ) -> TestCommandResult:
        started = time.perf_counter()
        command_text = " ".join(command)
        self._log.info(
            f"STARTED phase={phase} layer={layer or '-'} timeout_s={timeout:g} "
            f"command={command_text}"
        )
        executable = resolve_executable(command[0], self.environment)
        if executable is None:
            self._log.info(
                f"FINISHED phase={phase} layer={layer or '-'} status=ERROR "
                f"reason=command_unavailable"
            )
            return TestCommandResult(
                phase=phase,
                layer=layer,
                command=command,
                test_files=test_files,
                status="ERROR",
                returncode=None,
                duration_ms=round((time.perf_counter() - started) * 1000),
                error=f"ARC4506 TEST_COMMAND_UNAVAILABLE: {command[0]}",
            )
        actual_command = [executable, *command[1:]]
        capture_root = self.output_root / ".arc" / "runtime" / "test-output"
        capture_root.mkdir(parents=True, exist_ok=True)
        capture_id = f"{phase.lower()}-{str(layer or 'none').lower()}-{os.getpid()}-{time.time_ns()}"
        process_environment = dict(self.environment)
        is_e2e = str(layer or "").upper() == "E2E"
        if is_e2e:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                    listener.bind(("127.0.0.1", 0))
                    runtime_port = listener.getsockname()[1]
            except OSError as exc:
                return TestCommandResult(
                    phase=phase,
                    layer=layer,
                    command=command,
                    test_files=test_files,
                    status="ERROR",
                    returncode=None,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    error=f"ARC4509 E2E_PORT_ALLOCATION_FAILED: {exc}",
                )
            process_environment["ARC_E2E_PORT"] = str(runtime_port)
            process_environment["ARC_TEST_BASE_URL"] = f"http://127.0.0.1:{runtime_port}"
            debug_channels = [
                value.strip()
                for value in str(process_environment.get("DEBUG", "")).split(",")
                if value.strip()
            ]
            # Keep unrelated debug channels, but suppress Playwright's verbose
            # protocol/action stream, including when DEBUG contains a wildcard.
            debug_channels.append("-pw:*")
            process_environment["DEBUG"] = ",".join(debug_channels)
            process_environment["PLAYWRIGHT_LIST_PRINT_STEPS"] = "0"
            process_environment["FORCE_COLOR"] = "0"
        output_path = capture_root / f"{capture_id}.output.log"
        output_file = None
        try:
            output_file = output_path.open("w", encoding="utf-8", errors="replace")
            process = subprocess.Popen(
                actual_command,
                cwd=str(self.output_root),
                env=process_environment,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                **process_group_kwargs(),
            )
            output_file.close()
            output_file = None
        except OSError as exc:
            if output_file is not None:
                output_file.close()
            duration_ms = round((time.perf_counter() - started) * 1000)
            self._log.info(
                f"FINISHED phase={phase} layer={layer or '-'} status=ERROR "
                f"duration_ms={duration_ms}"
            )
            return TestCommandResult(
                phase=phase,
                layer=layer,
                command=command,
                test_files=test_files,
                status="ERROR",
                returncode=None,
                duration_ms=duration_ms,
                error=f"ARC4508 TEST_COMMAND_FAILED: {exc}",
            )
        try:
            process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_process_tree(process)
            try:
                process.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
            stdout = _read_capture_file(output_path)
            stderr = ""
            duration_ms = round((time.perf_counter() - started) * 1000)
            is_test_timeout = phase == "EXECUTION"
            status = "FAILED" if is_test_timeout else "ERROR"
            timeout_message = (
                f"TEST_COMMAND_TIMEOUT: {layer or phase} test command exceeded "
                f"{timeout:g}s and was terminated."
            )
            stderr = "\n".join(
                value for value in (stderr, timeout_message) if value
            )
            self._log.info(
                f"TIMED_OUT phase={phase} layer={layer or '-'} "
                f"status={status} duration_ms={duration_ms} timeout_s={timeout:g} "
                "process_tree=terminated"
            )
            return TestCommandResult(
                phase=phase,
                layer=layer,
                command=command,
                test_files=test_files,
                status=status,
                returncode=124 if is_test_timeout else None,
                duration_ms=duration_ms,
                stdout=stdout,
                stderr=stderr,
                error=(
                    None
                    if is_test_timeout
                    else f"ARC4507 TEST_COMMAND_TIMEOUT: exceeded {timeout:g}s."
                ),
            )
        except OSError as exc:
            terminate_process_tree(process)
            stdout = _read_capture_file(output_path)
            stderr = ""
            duration_ms = round((time.perf_counter() - started) * 1000)
            self._log.info(
                f"FINISHED phase={phase} layer={layer or '-'} status=ERROR "
                f"duration_ms={duration_ms}"
            )
            return TestCommandResult(
                phase=phase,
                layer=layer,
                command=command,
                test_files=test_files,
                status="ERROR",
                returncode=None,
                duration_ms=duration_ms,
                stdout=stdout,
                stderr=stderr,
                error=f"ARC4508 TEST_COMMAND_FAILED: {exc}",
            )
        stdout = _read_capture_file(output_path)
        stderr = ""
        duration_ms = round((time.perf_counter() - started) * 1000)
        status = "PASSED" if process.returncode == 0 else "FAILED"
        self._log.info(
            f"FINISHED phase={phase} layer={layer or '-'} status={status} "
            f"returncode={process.returncode} duration_ms={duration_ms}"
        )
        return TestCommandResult(
            phase=phase,
            layer=layer,
            command=command,
            test_files=test_files,
            status=status,
            returncode=process.returncode,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
        )

    def _finish(self, result: TestRunResult, started: float) -> TestRunResult:
        result.status = (
            "PASSED"
            if result.commands and all(row.status == "PASSED" for row in result.commands)
            else "FAILED"
        )
        result.errors.extend(
            row.error for row in result.commands if row.error is not None
        )
        result.errors = list(dict.fromkeys(result.errors))
        result.duration_ms = round((time.perf_counter() - started) * 1000)
        return result

    @staticmethod
    def _load_manifest(
        supplied: dict[str, Any] | None,
        path: Path,
        error_code: str,
    ) -> tuple[dict[str, Any], list[str]]:
        if supplied is not None:
            if isinstance(supplied, dict):
                return supplied, []
            return {}, [f"{error_code}: supplied value must be an object."]
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {}, [f"{error_code}: cannot read {path}: {exc}"]
        if not isinstance(value, dict):
            return {}, [f"{error_code}: {path} must contain an object."]
        return value, []


def _normalize_layers(values: tuple[str, ...]) -> tuple[list[str], list[str]]:
    requested = [str(value).strip().upper() for value in values if str(value).strip()]
    unknown = sorted(set(requested) - set(TEST_LAYERS))
    errors = (
        [f"ARC4503 TEST_SELECTION_INVALID: unknown test layers {unknown}."]
        if unknown
        else []
    )
    return [layer for layer in TEST_LAYERS if layer in requested], errors


def _execution_command(layer: str, test_files: list[str]) -> list[str]:
    workspace_files = [_test_workspace_path(value) for value in test_files]
    if layer == "INTEGRATION":
        return ["npm", "run", "test:integration", "-w", "@arc/tests", "--", *workspace_files]
    if layer == "UNIT":
        return [
            "npm",
            "exec",
            "-w",
            "@arc/tests",
            "--",
            "vitest",
            "run",
            "--config",
            "vitest.config.ts",
            *workspace_files,
        ]
    return [
        "npm",
        "exec",
        "-w",
        "@arc/tests",
        "--",
        "playwright",
        "test",
        "--config",
        "playwright.config.ts",
        "--project=chromium",
        # Override historical line/progress reporters without rewriting the
        # project's pinned test configuration. The list reporter preserves
        # failures, source locations, code frames and assertion differences.
        "--reporter=list",
        *workspace_files,
    ]


def _test_workspace_path(test_file: str) -> str:
    normalized = str(test_file).replace("\\", "/").strip().strip("/")
    return normalized.removeprefix("tests/")


def _read_capture_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""





def _bounded_float(
    environment: Mapping[str, str],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        return max(minimum, min(float(environment.get(name, str(default))), maximum))
    except ValueError:
        return default
