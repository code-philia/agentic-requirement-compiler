from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from core.logging import SynchronousLog

from .process_utils import (
    process_group_kwargs,
    resolve_executable,
    terminate_tcp_listeners,
    terminate_process_tree,
)
from .test_generation import TEST_ENVIRONMENT_READY, TEST_LAYERS, TESTS_FROZEN


TEST_RUNNER_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class TestSelection:
    """Select one requirement's frozen tests without repository discovery."""

    requirement_id: str
    layers: tuple[str, ...] = ()
    include_typecheck: bool = False
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
    timed_out: bool = False
    stub_hits: list[str] = field(default_factory=list)
    # Keep the complete process streams for the E2E report synthesizer. The
    # bounded stdout/stderr fields remain suitable for ordinary diagnostics.
    raw_stdout: str = ""
    raw_stderr: str = ""
    capture_stdout_path: str | None = None
    capture_stderr_path: str | None = None


@dataclass(slots=True)
class TestRunResult:
    requirement_id: str
    status: str
    selected_layers: list[str]
    selected_test_ids: list[str]
    selected_files: list[str]
    commands: list[TestCommandResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stub_hits: list[str] = field(default_factory=list)
    duration_ms: int = 0
    schema_version: int = TEST_RUNNER_SCHEMA_VERSION

    @property
    def ok(self) -> bool:
        return self.status == "PASSED" and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TestRunner:
    """Run an exact, integrity-checked test selection from the frozen manifest.

    Business-test failures may be collected across layers; typecheck failures
    still stop execution because later diagnostics would be misleading.
    """

    def __init__(
        self,
        output_root: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.output_root = output_root.expanduser().resolve()
        self.environment = dict(os.environ if environment is None else environment)
        self.environment.setdefault("CI", "1")
        # Each command gets its own absolute stub ledger in _execute. Reusing a
        # workspace-wide log lets another runner or a lingering server write
        # into the current command's evidence.
        self._log = SynchronousLog("TestRunner", workspace_root=self.output_root)
        self._timeouts = {
            "TYPECHECK": _bounded_float(
                self.environment,
                "ARC_TDD_TYPECHECK_TIMEOUT_SECONDS",
                300.0,
                30.0,
                900.0,
            ),
            "UNIT": _bounded_float(
                self.environment,
                "ARC_TDD_UNIT_TIMEOUT_SECONDS",
                120.0,
                10.0,
                900.0,
            ),
            "INTEGRATION": _bounded_float(
                self.environment,
                "ARC_TDD_INTEGRATION_TIMEOUT_SECONDS",
                180.0,
                10.0,
                900.0,
            ),
            "E2E": _bounded_float(
                self.environment,
                "ARC_TDD_E2E_TIMEOUT_SECONDS",
                180.0,
                60.0,
                1800.0,
            ),
        }

    def run(
        self,
        selection: TestSelection,
        *,
        test_manifest: dict[str, Any] | None = None,
        environment_manifest: dict[str, Any] | None = None,
    ) -> TestRunResult:
        """Run only the selected requirement files, stopping at the first failed layer."""

        started = time.perf_counter()
        requirement_id = str(selection.requirement_id).strip()
        manifest, manifest_errors = self._load_manifest(
            test_manifest,
            self.output_root / ".arc" / "tests" / "test_manifest.json",
            "ARC4501 TEST_MANIFEST_INVALID",
        )
        environment, environment_errors = self._load_manifest(
            environment_manifest,
            self.output_root / ".arc" / "tests" / "environment_manifest.json",
            "ARC4502 TEST_ENVIRONMENT_INVALID",
        )
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
        if manifest.get("environment_status") not in {None, TEST_ENVIRONMENT_READY}:
            errors.append(
                "ARC4501 TEST_MANIFEST_INVALID: frozen tests reference an invalid environment."
            )
        freeze_policy = manifest.get("freeze_policy")
        if not isinstance(freeze_policy, dict) or (
            freeze_policy.get("tests_are_read_only_during_implementation") is not True
            or freeze_policy.get("integrity") != "SHA256"
        ):
            errors.append(
                "ARC4501 TEST_MANIFEST_INVALID: frozen test integrity policy is missing."
            )

        layers, layer_errors = _normalize_layers(selection.layers)
        errors.extend(layer_errors)
        file_rows = [
            row
            for row in manifest.get("files", [])
            if isinstance(row, dict)
            and str(row.get("requirement_id", "")) == requirement_id
        ]
        requirement_rows = [
            row
            for row in manifest.get("requirements", [])
            if isinstance(row, dict)
            and str(row.get("requirement_id", "")) == requirement_id
        ]
        if len(requirement_rows) != 1 or requirement_rows[0].get("state") != TESTS_FROZEN:
            errors.append(
                f"ARC4503 TEST_SELECTION_INVALID: {requirement_id!r} has no frozen manifest slice."
            )
        available_layers = [
            layer
            for layer in TEST_LAYERS
            if any(str(row.get("layer", "")).upper() == layer for row in file_rows)
        ]
        selected_layers = layers or available_layers
        backend_port = _environment_backend_port(environment)
        if "E2E" in selected_layers and backend_port is None:
            errors.append(
                "ARC4502 TEST_ENVIRONMENT_INVALID: backend_port must be an integer "
                "between 1 and 65535 before E2E execution."
            )
        missing_layers = [layer for layer in selected_layers if layer not in available_layers]
        if missing_layers:
            errors.append(
                "ARC4503 TEST_SELECTION_INVALID: requested layers are unavailable for "
                f"{requirement_id}: {missing_layers}."
            )
        selected_rows = [
            row
            for layer in TEST_LAYERS
            if layer in selected_layers
            for row in file_rows
            if str(row.get("layer", "")).upper() == layer
        ]
        if not selected_rows:
            errors.append(
                f"ARC4503 TEST_SELECTION_INVALID: no frozen tests selected for {requirement_id}."
            )
        duplicate_layers = [
            layer
            for layer in selected_layers
            if sum(
                str(row.get("layer", "")).upper() == layer
                for row in selected_rows
            ) != 1
        ]
        if duplicate_layers:
            errors.append(
                "ARC4501 TEST_MANIFEST_INVALID: expected exactly one frozen file for "
                f"each selected layer, invalid layers: {duplicate_layers}."
            )

        manifest_tests = [
            row
            for row in manifest.get("tests", [])
            if isinstance(row, dict)
            and str(row.get("requirement_id", "")) == requirement_id
            and str(row.get("layer", "")).upper() in selected_layers
        ]
        expected_test_ids = {
            str(row.get("test_id", ""))
            for row in manifest_tests
            if str(row.get("test_id", ""))
        }
        file_test_ids = {
            str(value)
            for row in selected_rows
            for value in row.get("test_ids", [])
            if str(value)
        }
        if not expected_test_ids or file_test_ids != expected_test_ids:
            errors.append(
                "ARC4501 TEST_MANIFEST_INVALID: selected file rows and test rows disagree "
                f"for {requirement_id}."
            )
        elif any(
            str(row.get("test_file", "")) not in {
                str(file_row.get("test_file", "")) for file_row in selected_rows
            }
            for row in manifest_tests
        ):
            errors.append(
                "ARC4501 TEST_MANIFEST_INVALID: a selected test references an unexpected file."
            )

        selected_files: list[str] = []
        selected_test_ids: list[str] = []
        for row in selected_rows:
            file_error = self._validate_frozen_file(row)
            if file_error:
                errors.append(file_error)
                continue
            selected_files.append(str(row["test_file"]))
            selected_test_ids.extend(str(value) for value in row.get("test_ids", []))

        result = TestRunResult(
            requirement_id=requirement_id,
            status="INVALID_SELECTION" if errors else "RUNNING",
            selected_layers=selected_layers,
            selected_test_ids=sorted(set(selected_test_ids)),
            selected_files=selected_files,
            errors=list(dict.fromkeys(errors)),
        )
        if errors:
            result.duration_ms = round((time.perf_counter() - started) * 1000)
            return result

        if selection.include_typecheck:
            typecheck = self._execute(
                phase="TYPECHECK",
                layer=None,
                command=["npm", "run", "typecheck"],
                test_files=selected_files,
                timeout=self._timeouts["TYPECHECK"],
            )
            result.commands.append(typecheck)
            # A failed typecheck invalidates subsequent test results; do not
            # execute them merely because business-test collection is enabled.
            if typecheck.status != "PASSED":
                return self._finish(result, started)

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
                timeout=self._timeouts[layer],
                e2e_port=backend_port if layer == "E2E" else None,
            )
            result.commands.append(command_result)
            if command_result.status != "PASSED" and selection.stop_on_failure:
                break
        return self._finish(result, started)

    def run_workspace_typecheck(self) -> TestCommandResult:
        """Type-check every generated workspace after any implementation patch."""

        return self._execute(
            phase="TYPECHECK",
            layer=None,
            command=["npm", "run", "typecheck"],
            test_files=[],
            timeout=self._timeouts["TYPECHECK"],
        )

    def _validate_frozen_file(self, row: dict[str, Any]) -> str | None:
        relative = str(row.get("test_file", "")).replace("\\", "/").strip().strip("/")
        layer = str(row.get("layer", "")).upper()
        path = PurePosixPath(relative)
        expected_root = f"tests/{layer.lower()}/"
        if (
            row.get("status") != "FROZEN"
            or layer not in TEST_LAYERS
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
        e2e_port: int | None = None,
    ) -> TestCommandResult:
        started = time.perf_counter()
        command_text = " ".join(command)
        self._log.info(
            f"STARTED phase={phase} layer={layer or '-'} timeout_s={timeout:g} "
            f"command={command_text}"
        )
        if str(layer or "").upper() == "E2E" and e2e_port is not None:
            terminated_pids, cleanup_errors = terminate_tcp_listeners(e2e_port)
            if terminated_pids:
                self._log.info(
                    "STALE_E2E_SERVER_TERMINATED "
                    f"port={e2e_port} pids={terminated_pids}"
                )
            if cleanup_errors:
                duration_ms = round((time.perf_counter() - started) * 1000)
                detail = "; ".join(cleanup_errors)
                self._log.info(
                    "E2E_PORT_CLEANUP_FAILED "
                    f"port={e2e_port} duration_ms={duration_ms} errors={detail}"
                )
                return TestCommandResult(
                    phase=phase,
                    layer=layer,
                    command=command,
                    test_files=test_files,
                    status="ERROR",
                    returncode=None,
                    duration_ms=duration_ms,
                    error=(
                        "ARC4509 E2E_PORT_CLEANUP_FAILED: cannot release TCP port "
                        f"{e2e_port}: {detail}"
                    ),
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
        stub_log_path = capture_root / f"{capture_id}.stub-hits.log"
        process_environment = dict(self.environment)
        process_environment["ARC_STUB_LOG"] = str(stub_log_path)
        if str(layer or "").upper() == "E2E":
            debug_channels = [
                value.strip()
                for value in str(process_environment.get("DEBUG", "")).split(",")
                if value.strip()
            ]
            if "pw:api" not in debug_channels:
                debug_channels.append("pw:api")
            process_environment["DEBUG"] = ",".join(debug_channels)
        stdout_path = capture_root / f"{capture_id}.stdout.log"
        stderr_path = capture_root / f"{capture_id}.stderr.log"
        stdout_file = None
        stderr_file = None
        try:
            stdout_file = stdout_path.open("w", encoding="utf-8", errors="replace")
            stderr_file = stderr_path.open("w", encoding="utf-8", errors="replace")
            process = subprocess.Popen(
                actual_command,
                cwd=str(self.output_root),
                env=process_environment,
                stdout=stdout_file,
                stderr=stderr_file,
                **process_group_kwargs(),
            )
            stdout_file.close()
            stderr_file.close()
            stdout_file = None
            stderr_file = None
        except OSError as exc:
            for capture_handle in (stdout_file, stderr_file):
                if capture_handle is not None:
                    capture_handle.close()
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
                capture_stdout_path=str(stdout_path),
                capture_stderr_path=str(stderr_path),
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
            stdout = _read_capture_file(stdout_path)
            stderr = _read_capture_file(stderr_path)
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
                stdout=_bounded_output(stdout),
                stderr=_bounded_output(stderr),
                error=(
                    None
                    if is_test_timeout
                    else f"ARC4507 TEST_COMMAND_TIMEOUT: exceeded {timeout:g}s."
                ),
                timed_out=True,
                stub_hits=self._collect_stub_hits(stub_log_path),
                raw_stdout=stdout,
                raw_stderr=stderr,
                capture_stdout_path=str(stdout_path),
                capture_stderr_path=str(stderr_path),
            )
        except OSError as exc:
            terminate_process_tree(process)
            stdout = _read_capture_file(stdout_path)
            stderr = _read_capture_file(stderr_path)
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
                stdout=_bounded_output(stdout),
                stderr=_bounded_output(stderr),
                error=f"ARC4508 TEST_COMMAND_FAILED: {exc}",
                raw_stdout=stdout,
                raw_stderr=stderr,
                capture_stdout_path=str(stdout_path),
                capture_stderr_path=str(stderr_path),
            )
        stdout = _read_capture_file(stdout_path)
        stderr = _read_capture_file(stderr_path)
        duration_ms = round((time.perf_counter() - started) * 1000)
        stub_hits = self._collect_stub_hits(stub_log_path)
        status = "PASSED" if process.returncode == 0 and not stub_hits else "FAILED"
        if process.returncode == 0 and stub_hits:
            stub_message = (
                "ARC4513 UNIMPLEMENTED_MODULE_EXECUTED: test assertions passed but "
                f"runtime skeletons were executed: {stub_hits}."
            )
            stderr = "\n".join(value for value in (stderr, stub_message) if value)
        self._log.info(
            f"FINISHED phase={phase} layer={layer or '-'} status={status} "
            f"returncode={process.returncode} duration_ms={duration_ms} "
            f"stub_hits={len(stub_hits)}"
        )
        return TestCommandResult(
            phase=phase,
            layer=layer,
            command=command,
            test_files=test_files,
            status=status,
            returncode=process.returncode,
            duration_ms=duration_ms,
            stdout=_bounded_output(stdout),
            stderr=_bounded_output(stderr),
            stub_hits=stub_hits,
            raw_stdout=stdout or "",
            raw_stderr=stderr or "",
            capture_stdout_path=str(stdout_path),
            capture_stderr_path=str(stderr_path),
        )

    def _collect_stub_hits(self, stub_log_path: Path) -> list[str]:
        """Read the module ids that answered from a skeleton during this command."""

        try:
            raw = stub_log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return sorted({line.strip() for line in raw.splitlines() if line.strip()})

    def _finish(self, result: TestRunResult, started: float) -> TestRunResult:
        result.stub_hits = sorted(
            {value for row in result.commands for value in row.stub_hits}
        )
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
    if layer in {"UNIT", "INTEGRATION"}:
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
        "--reporter=line,json",
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


def _bounded_output(value: str | bytes | None, limit: int = 100_000) -> str:
    """Retain enough runner output for assertion diagnostics and agent repair."""
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    return text[-limit:]


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


def _environment_backend_port(environment: Mapping[str, Any]) -> int | None:
    try:
        port = int(environment.get("backend_port", 0))
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None
