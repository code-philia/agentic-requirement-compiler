from __future__ import annotations

import copy
import hashlib
import json
import os
import posixpath
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from core.logging import SynchronousLog

from .artifacts import CompilerArtifactStore
from .code_binding import CODE_BINDING_READY, CodeTargetResolver
from .database_stage import schema_for_requirement
from .model_client import StructuredModel, describe_model_error
from .project_initialization import DependencyCatalog, test_workspace_spec
from .process_utils import resolve_executable, run_command
from .trace_payload import format_payload_trace


TEST_ENVIRONMENT_READY = "TEST_ENVIRONMENT_READY"
TESTS_FROZEN = "TESTS_FROZEN"
TEST_LAYERS = ("UNIT", "INTEGRATION", "E2E")
TEST_GENERATION_SCHEMA_VERSION = 1


TEST_GENERATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["files"],
    "properties": {
        "files": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["layer", "code"],
                "properties": {
                    "layer": {"type": "string", "enum": list(TEST_LAYERS)},
                    "code": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}
TEST_GENERATION_INSTRUCTIONS = """Write executable requirement tests using the original requirement
and the supplied source files. Assertions must come from the requirement and its
scenarios, never from skeleton placeholders or current implementation behavior.
Use the exact exports, helper signatures, database fields and routes in the supplied
code. Generate exactly one complete TypeScript file for each supplied layer, with
real assertions over inputs, outputs and persisted data. Keep each test independent;
use fresh unique values and seed fixtures in beforeEach when required. E2E locators
must match real accessible UI controls; do not treat descriptions as literal labels.
Do not edit application code, invent paths, skip tests or weaken assertions to pass.
Return only JSON: {"files":[{"layer":"UNIT|INTEGRATION|E2E","code":"..."}]}.
"""
@dataclass(slots=True)
class TestEnvironmentResult:
    ok: bool
    manifest: dict[str, Any]
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TestStaticValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    source_diagnostics: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TestGenerationResult:
    manifest: dict[str, Any]
    node_states: dict[str, str]
    artifacts: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    source_diagnostics: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class TestEnvironmentInitializer:
    """Validate the fixed test workspace provisioned during Project Initialization."""

    def __init__(
        self,
        output_root: Path,
        *,
        backend_port: int,
        catalog: DependencyCatalog | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.output_root = output_root.expanduser().resolve()
        self.tests_root = self.output_root / "tests"
        self.backend_port = max(1, min(65535, int(backend_port)))
        self.catalog = catalog or DependencyCatalog()
        self.environment = dict(os.environ if environment is None else environment)

    def initialize(self) -> TestEnvironmentResult:
        errors: list[str] = []
        spec = test_workspace_spec(self.catalog, backend_port=self.backend_port)
        browser_installed = False
        environment_source = "PROJECT_MANIFEST"
        try:
            root_package = _read_json_object(self.output_root / "package.json")
            test_package = _read_json_object(self.tests_root / "package.json")
            test_tsconfig = _read_json_object(self.tests_root / "tsconfig.json")
            project_manifest = _read_json_object(
                self.output_root / ".arc" / "project" / "project-manifest.json"
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"ARC4401 TEST_ENVIRONMENT_INVALID: {exc}")
            root_package = {}
            test_package = {}
            test_tsconfig = {}
            project_manifest = {}

        if test_package != spec["package"]:
            errors.append(
                "ARC4401 TEST_ENVIRONMENT_INVALID: tests/package.json differs from "
                "the compiler-owned pinned workspace."
            )
        if test_tsconfig != spec["tsconfig"]:
            errors.append(
                "ARC4401 TEST_ENVIRONMENT_INVALID: tests/tsconfig.json differs from "
                "the compiler-owned configuration."
            )
        for directory in ("unit", "integration", "e2e", "support"):
            if not (self.tests_root / directory).is_dir():
                errors.append(
                    f"ARC4401 TEST_ENVIRONMENT_INVALID: missing tests/{directory}."
                )
        for relative, expected in spec["text_files"].items():
            path = self.tests_root / relative
            try:
                actual = path.read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(
                    f"ARC4401 TEST_ENVIRONMENT_INVALID: cannot read tests/{relative}: {exc}"
                )
                continue
            if actual != expected:
                errors.append(
                    f"ARC4401 TEST_ENVIRONMENT_INVALID: tests/{relative} differs from "
                    "the compiler-owned configuration."
                )

        workspaces = root_package.get("workspaces")
        if not isinstance(workspaces, list) or "tests" not in workspaces:
            errors.append(
                "ARC4401 TEST_ENVIRONMENT_INVALID: root package workspaces must contain tests."
            )
        expected_scripts = {
            "test:typecheck": "npm run typecheck",
            "test:list": (
                "npm run list:vitest -w @arc/tests && npm run list:e2e -w @arc/tests"
            ),
            "test:unit": "npm run test:unit -w @arc/tests",
            "test:integration": "npm run test:integration -w @arc/tests",
            "test:e2e": "npm run test:e2e -w @arc/tests",
        }
        root_scripts = root_package.get("scripts")
        if not isinstance(root_scripts, dict) or any(
            root_scripts.get(name) != command
            for name, command in expected_scripts.items()
        ):
            errors.append(
                "ARC4401 TEST_ENVIRONMENT_INVALID: root package test scripts are incomplete."
            )
        if not (self.output_root / "package-lock.json").is_file() or not (
            self.output_root / "node_modules"
        ).is_dir():
            errors.append(
                "ARC4401 TEST_ENVIRONMENT_INVALID: the initialized npm workspace is unavailable."
            )
        tests_link = self.output_root / "node_modules" / "@arc" / "tests"
        expected_tests = self.tests_root.resolve()
        actual_tests = tests_link.resolve()
        if not tests_link.exists() or actual_tests != expected_tests:
            errors.append(
                "ARC4401 TEST_ENVIRONMENT_INVALID: @arc/tests workspace link is invalid."
            )
        for package_path in ("vitest", "supertest", "typescript", "@playwright/test"):
            if not (self.output_root / "node_modules" / package_path).exists():
                errors.append(
                    "ARC4401 TEST_ENVIRONMENT_INVALID: preinstalled package is missing: "
                    f"{package_path}."
                )

        project_test_environment = project_manifest.get("testEnvironment")
        if not isinstance(project_test_environment, dict):
            legacy_manifest_path = (
                self.output_root / ".arc" / "tests" / "environment_manifest.json"
            )
            try:
                legacy_manifest = _read_json_object(legacy_manifest_path)
            except (OSError, ValueError, json.JSONDecodeError):
                legacy_manifest = {}
            if legacy_manifest.get("status") == TEST_ENVIRONMENT_READY:
                project_test_environment = {
                    "status": legacy_manifest.get("status"),
                    "browserInstalled": legacy_manifest.get("browser_installed", False),
                    "versions": legacy_manifest.get("versions"),
                }
                environment_source = "LEGACY_TEST_ENVIRONMENT_MANIFEST"
        if not isinstance(project_test_environment, dict) or project_test_environment.get(
            "status"
        ) != TEST_ENVIRONMENT_READY:
            errors.append(
                "ARC4401 TEST_ENVIRONMENT_INVALID: Project Manifest has no ready test environment."
            )
        else:
            browser_installed = bool(project_test_environment.get("browserInstalled"))
            if project_test_environment.get("versions") != {
                "typescript": self.catalog.typescript,
                "vitest": self.catalog.vitest,
                "@playwright/test": self.catalog.playwright,
                "supertest": self.catalog.supertest,
                "@types/supertest": self.catalog.types_supertest,
            }:
                errors.append(
                    "ARC4401 TEST_ENVIRONMENT_INVALID: Project Manifest test versions differ "
                    "from the compiler dependency catalog."
                )

        manifest = {
            "schema_version": 1,
            "status": TEST_ENVIRONMENT_READY if not errors else "TEST_ENVIRONMENT_FAILED",
            "workspace": "tests",
            "package": "@arc/tests",
            "frameworks": {
                "unit": "vitest",
                "integration": "vitest+supertest",
                "e2e": "playwright",
            },
            "roots": {
                "unit": "tests/unit",
                "integration": "tests/integration",
                "e2e": "tests/e2e",
                "support": "tests/support",
            },
            "validation_commands": [
                "npm run typecheck",
                "npm run list:vitest -w @arc/tests",
                "npm run list:e2e -w @arc/tests",
            ],
            "execution_commands": {
                "unit": "npm run test:unit -w @arc/tests",
                "integration": "npm run test:integration -w @arc/tests",
                "e2e": "npm run test:e2e -w @arc/tests",
            },
            "browser_installed": browser_installed,
            "provisioned_by": (
                "PROJECT_INITIALIZATION"
                if environment_source == "PROJECT_MANIFEST"
                else "LEGACY_TEST_GENERATION"
            ),
            "reused_without_install": True,
            "validated_from": environment_source,
            "workspace_occurrences": (
                workspaces.count("tests") if isinstance(workspaces, list) else 0
            ),
            "backend_port": self.backend_port,
            "frontend_port": self.backend_port,
            "versions": {
                "typescript": self.catalog.typescript,
                "vitest": self.catalog.vitest,
                "@playwright/test": self.catalog.playwright,
                "supertest": self.catalog.supertest,
                "@types/supertest": self.catalog.types_supertest,
            },
        }
        return TestEnvironmentResult(ok=not errors, manifest=manifest, errors=errors)


class TestStaticValidator:
    """Check generated tests without executing their assertions."""

    def __init__(
        self,
        output_root: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.output_root = output_root.expanduser().resolve()
        self.environment = dict(os.environ if environment is None else environment)
        self._timeout = _bounded_float_env(
            self.environment,
            "ARC_TEST_VALIDATION_TIMEOUT_SECONDS",
            300.0,
            30.0,
            900.0,
        )

    def validate(
        self,
        *,
        has_vitest: bool,
        has_e2e: bool,
        test_files: list[str] | None = None,
    ) -> TestStaticValidationResult:
        # Validate generated test types, not the whole application. The TDD
        # implementation has already changed application sources at this point.
        commands: list[list[str]] = [
            ["npm", "run", "typecheck", "-w", "@arc/tests"],
        ]
        selected_files = [
            _test_workspace_path(value)
            for value in (test_files or [])
        ]
        vitest_files = [
            value
            for value in selected_files
            if value.startswith(("unit/", "integration/"))
        ]
        e2e_files = [value for value in selected_files if value.startswith("e2e/")]
        if has_vitest:
            command = ["npm", "run", "list:vitest", "-w", "@arc/tests"]
            if vitest_files:
                command.extend(["--", *vitest_files])
            commands.append(command)
        if has_e2e:
            command = ["npm", "run", "list:e2e", "-w", "@arc/tests"]
            if e2e_files:
                command.extend(["--", *e2e_files])
            commands.append(command)
        errors: list[str] = []
        source_diagnostics: list[str] = []
        for command in commands:
            executable = resolve_executable(command[0], self.environment)
            if executable is None:
                errors.append(
                    f"ARC4431 TEST_STATIC_VALIDATION_FAILED: command unavailable: {command[0]}"
                )
                break
            try:
                completed = run_command(
                    [executable, *command[1:]],
                    cwd=str(self.output_root),
                    environment=self.environment,
                    timeout=self._timeout,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"ARC4431 TEST_STATIC_VALIDATION_FAILED: {exc}")
                break
            if completed.returncode != 0:
                output = "\n".join(
                    part for part in (completed.stdout or "", completed.stderr or "")
                    if part
                )
                if command == commands[0]:
                    application_errors = _application_only_type_errors(output)
                    if application_errors:
                        source_diagnostics.extend(application_errors)
                        continue
                errors.append(
                    "ARC4431 TEST_STATIC_VALIDATION_FAILED: "
                    f"{command!r} exited with {completed.returncode}: "
                    f"{_command_output(completed.stdout, completed.stderr)}"
                )
                break
        return TestStaticValidationResult(
            ok=not errors,
            errors=errors,
            source_diagnostics=source_diagnostics,
        )


def _format_model_log(payload: dict[str, Any]) -> str:
    sections = [
        "ARC MODEL INVOCATION",
        f"schema_name: {payload.get('schema_name', '')}",
        f"requirement_id: {payload.get('requirement_id', '')}",
        f"iteration: {payload.get('iteration', '')}",
        f"attempt: {payload.get('attempt', '')}",
        f"duration_ms: {payload.get('duration_ms', '')}",
        "",
        "===== INSTRUCTIONS =====",
        str(payload.get("instructions", "")),
        "",
        "===== INPUT PAYLOAD =====",
        json.dumps(payload.get("input_payload", {}), ensure_ascii=False, indent=2, default=str),
        "",
        "===== OUTPUT SCHEMA =====",
        json.dumps(payload.get("output_schema", {}), ensure_ascii=False, indent=2, default=str),
        "",
        "===== MODEL OUTPUT =====",
        json.dumps(payload.get("output"), ensure_ascii=False, indent=2, default=str)
        if payload.get("output") is not None
        else "(no parsed model output)",
        "",
        "===== ERROR =====",
        str(payload.get("error") or "(none)"),
        "",
    ]
    return "\n".join(sections)


class RequirementTestGenerationPass:
    """Generate and freeze a bounded set of tests for each atomic requirement."""

    def __init__(
        self,
        model: StructuredModel,
        output_root: Path,
        artifact_store: CompilerArtifactStore,
    ) -> None:
        self._model = model
        self._output_root = output_root.expanduser().resolve()
        self._artifact_store = artifact_store
        self._validator = TestStaticValidator(self._output_root)
        self._retries = _bounded_int_env("ARC_TEST_GENERATION_RETRY_COUNT", 2, 0, 5)
        self._trace_enabled = _env_flag(os.environ, "ARC_TEST_GENERATION_TRACE", True)
        self._log = SynchronousLog(
            "RequirementTestGenerationPass", workspace_root=self._output_root
        )

    def _write_model_log(self, payload: dict[str, Any]) -> None:
        log_root = self._output_root / ".arc" / "model_logs" / "test_generation"
        log_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"{time.time_ns() % 1_000_000_000:09d}Z"
        requirement_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(payload.get("requirement_id", "unknown")))
        attempt = int(payload.get("attempt", 0) or 0)
        path = log_root / f"{stamp}-{requirement_id}-attempt-{attempt}.log"
        path.write_text(_format_model_log(payload), encoding="utf-8")

    def compile(
        self,
        *,
        requirement_ir: dict[str, Any],
        dependency_graph: dict[str, Any],
        database_schema: dict[str, Any],
        design_ir: dict[str, Any],
        frontend_ir: dict[str, Any],
        code_binding_registry: dict[str, Any],
        environment_manifest: dict[str, Any],
    ) -> TestGenerationResult:
        global_errors: list[str] = []
        if code_binding_registry.get("status") != CODE_BINDING_READY:
            global_errors.append(
                "ARC4410 CODE_BINDING_NOT_READY: Test Generation requires CODE_BINDING_READY."
            )
        if environment_manifest.get("status") != TEST_ENVIRONMENT_READY:
            global_errors.append(
                "ARC4411 TEST_ENVIRONMENT_NOT_READY: Test environment is unavailable."
            )
        if global_errors:
            manifest = _finalize_manifest(
                _empty_test_manifest("TEST_GENERATION_FAILED"),
                status="TEST_GENERATION_FAILED",
                requirement_order=[],
                node_states={},
                environment_manifest=environment_manifest,
                code_binding_registry=code_binding_registry,
            )
            artifact = self._artifact_store.write_test_manifest(manifest)
            return TestGenerationResult(
                manifest=manifest,
                node_states={},
                artifacts={"test_manifest": artifact},
                errors=global_errors,
            )

        order = _atomic_order(requirement_ir, dependency_graph)
        states = {requirement_id: "TEST_DISCOVERED" for requirement_id in order}
        artifacts: dict[str, str] = {}
        errors: list[str] = []
        manifest = _empty_test_manifest("TEST_GENERATING")

        for requirement_id in order:
            result = self.generate_requirement(
                requirement_id=requirement_id,
                requirement_ir=requirement_ir,
                database_schema=database_schema,
                design_ir=design_ir,
                frontend_ir=frontend_ir,
                code_binding_registry=code_binding_registry,
                environment_manifest=environment_manifest,
                existing_manifest=manifest,
            )
            manifest = result.manifest
            states.update(result.node_states)
            artifacts.update(result.artifacts)
            errors.extend(result.errors)
            if not result.ok:
                break

        manifest = _finalize_manifest(
            manifest,
            status=TESTS_FROZEN if not errors else "TEST_GENERATION_FAILED",
            requirement_order=order,
            node_states=states,
            environment_manifest=environment_manifest,
            code_binding_registry=code_binding_registry,
        )
        artifacts["test_manifest"] = self._artifact_store.write_test_manifest(manifest)
        return TestGenerationResult(
            manifest=manifest,
            node_states=states,
            artifacts=artifacts,
            errors=list(dict.fromkeys(errors)),
        )

    def generate_requirement(
        self,
        *,
        requirement_id: str,
        requirement_ir: dict[str, Any],
        database_schema: dict[str, Any],
        design_ir: dict[str, Any],
        frontend_ir: dict[str, Any],
        code_binding_registry: dict[str, Any],
        environment_manifest: dict[str, Any],
        existing_manifest: dict[str, Any] | None = None,
    ) -> TestGenerationResult:
        """Generate, validate, and freeze tests for exactly one atomic requirement.

        When an existing manifest is supplied, its other requirement slices are
        retained and the selected requirement slice is replaced atomically at
        the manifest level. This is the Stage 5 node-by-node entry point.
        """

        requirement_id = str(requirement_id).strip()
        base_manifest = copy.deepcopy(
            existing_manifest
            if isinstance(existing_manifest, dict)
            else _empty_test_manifest("TEST_GENERATING")
        )
        state = {requirement_id: "TEST_DISCOVERED"} if requirement_id else {}
        precondition_errors = _test_generation_precondition_errors(
            requirement_id=requirement_id,
            requirement_ir=requirement_ir,
            code_binding_registry=code_binding_registry,
            environment_manifest=environment_manifest,
        )
        if precondition_errors:
            if requirement_id:
                state[requirement_id] = "FAILED"
            manifest = _finalize_manifest(
                base_manifest,
                status="TEST_GENERATION_FAILED",
                requirement_order=[requirement_id] if requirement_id else [],
                node_states=state,
                environment_manifest=environment_manifest,
                code_binding_registry=code_binding_registry,
            )
            artifact = self._artifact_store.write_test_manifest(manifest)
            return TestGenerationResult(
                manifest=manifest,
                node_states=state,
                artifacts={"test_manifest": artifact},
                errors=precondition_errors,
            )

        nodes = requirement_ir.get("nodes", {})
        node = nodes[requirement_id]
        try:
            resolved_targets = CodeTargetResolver(
                code_binding_registry
            ).resolve_requirement_targets(requirement_id)
        except KeyError as exc:
            state[requirement_id] = "FAILED"
            errors = [f"ARC4412 TEST_CONTEXT_INVALID: {exc}"]
            manifest = _finalize_manifest(
                base_manifest,
                status="TEST_GENERATION_FAILED",
                requirement_order=[requirement_id],
                node_states=state,
                environment_manifest=environment_manifest,
                code_binding_registry=code_binding_registry,
            )
            artifact = self._artifact_store.write_test_manifest(manifest)
            return TestGenerationResult(
                manifest=manifest,
                node_states=state,
                artifacts={"test_manifest": artifact},
                errors=errors,
            )

        test_obligations = _plan_test_obligations(
            requirement_id,
            resolved_targets,
            design_ir,
        )
        required_layers = [layer for layer in TEST_LAYERS if layer in test_obligations]
        if not required_layers:
            state[requirement_id] = "FAILED"
            errors = [
                f"ARC4413 TEST_LAYER_UNRESOLVED: no public test seam for {requirement_id}."
            ]
            manifest = _finalize_manifest(
                base_manifest,
                status="TEST_GENERATION_FAILED",
                requirement_order=[requirement_id],
                node_states=state,
                environment_manifest=environment_manifest,
                code_binding_registry=code_binding_registry,
            )
            artifact = self._artifact_store.write_test_manifest(manifest)
            return TestGenerationResult(
                manifest=manifest,
                node_states=state,
                artifacts={"test_manifest": artifact},
                errors=errors,
            )

        context_pack, validation_context = _build_context_pack(
            output_root=self._output_root,
            requirement_id=requirement_id,
            requirement=node,
            database_schema=database_schema,
            design_ir=design_ir,
            frontend_ir=frontend_ir,
            resolved_targets=resolved_targets,
            required_layers=required_layers,
            test_obligations=test_obligations,
        )
        artifacts: dict[str, str] = {}
        decision, sources, errors, source_diagnostics = self._generate_and_validate(
            requirement_id,
            context_pack,
            validation_context,
        )
        if decision is None:
            state[requirement_id] = "FAILED"
            manifest = _finalize_manifest(
                base_manifest,
                status="TEST_GENERATION_FAILED",
                requirement_order=[requirement_id],
                node_states=state,
                environment_manifest=environment_manifest,
                code_binding_registry=code_binding_registry,
            )
            artifacts["test_manifest"] = self._artifact_store.write_test_manifest(manifest)
            return TestGenerationResult(
                manifest=manifest,
                node_states=state,
                artifacts=artifacts,
                errors=list(dict.fromkeys(errors)),
            )

        previous_paths = {
            str(row.get("test_file", ""))
            for row in base_manifest.get("files", [])
            if isinstance(row, dict)
            and str(row.get("requirement_id", "")) == requirement_id
            and str(row.get("test_file", ""))
        }
        self._remove_requirement_files(previous_paths - set(sources))
        artifacts.update(self._artifact_store.write_generated_tests(sources))
        test_rows, file_rows = _manifest_rows(
            requirement_id,
            node,
            decision,
            validation_context,
        )
        manifest = _replace_requirement_slice(
            base_manifest,
            requirement_id=requirement_id,
            state=TESTS_FROZEN,
            test_rows=test_rows,
            file_rows=file_rows,
        )
        manifest = _finalize_manifest(
            manifest,
            status=TESTS_FROZEN,
            requirement_order=_manifest_requirement_order(manifest),
            node_states={requirement_id: TESTS_FROZEN},
            environment_manifest=environment_manifest,
            code_binding_registry=code_binding_registry,
        )
        state[requirement_id] = TESTS_FROZEN
        artifacts["test_manifest"] = self._artifact_store.write_test_manifest(manifest)
        return TestGenerationResult(
            manifest=manifest,
            node_states=state,
            artifacts=artifacts,
            source_diagnostics=source_diagnostics,
        )

    def _generate_and_validate(
        self,
        requirement_id: str,
        context_pack: dict[str, Any],
        validation_context: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, str], list[str], list[str]]:
        feedback: list[str] = []
        last_errors: list[str] = []
        last_decision: dict[str, Any] | None = None
        planned_paths = set(validation_context["output_files"].values())
        for attempt in range(self._retries + 1):
            payload = copy.deepcopy(context_pack)
            if feedback:
                payload["materialization_feedback"] = feedback
            if last_decision is not None:
                payload["previous_decision"] = copy.deepcopy(last_decision)
            self._trace(
                f"MODEL_REQUEST requirement={requirement_id} "
                f"attempt={attempt + 1}/{self._retries + 1}"
            )
            self._trace(_context_audit(requirement_id, attempt + 1, payload))
            self._trace_json("MODEL_INPUT", requirement_id, payload)
            started = time.perf_counter()
            try:
                model_started = time.perf_counter()
                decision = self._model.generate_json(
                    schema_name="arc_requirement_tests",
                    instructions=TEST_GENERATION_INSTRUCTIONS,
                    input_payload=payload,
                    output_schema=TEST_GENERATION_SCHEMA,
                )
                try:
                    self._write_model_log({
                    "schema_name": "arc_requirement_tests",
                    "instructions": TEST_GENERATION_INSTRUCTIONS,
                    "input_payload": payload,
                    "output_schema": TEST_GENERATION_SCHEMA,
                    "output": decision,
                    "error": None,
                    "duration_ms": round((time.perf_counter() - model_started) * 1000),
                    "attempt": attempt + 1,
                    "requirement_id": requirement_id,
                    })
                except Exception as log_exc:
                    self._trace(f"MODEL_LOG_WRITE_FAILED: {type(log_exc).__name__}: {log_exc}")
            except Exception as exc:
                try:
                    self._write_model_log({
                    "schema_name": "arc_requirement_tests",
                    "instructions": TEST_GENERATION_INSTRUCTIONS,
                    "input_payload": payload,
                    "output_schema": TEST_GENERATION_SCHEMA,
                    "output": None,
                    "error": describe_model_error(exc),
                    "duration_ms": round((time.perf_counter() - model_started) * 1000)
                    if "model_started" in locals() else None,
                    "attempt": attempt + 1,
                    "requirement_id": requirement_id,
                    })
                except Exception as log_exc:
                    self._trace(f"MODEL_LOG_WRITE_FAILED: {type(log_exc).__name__}: {log_exc}")
                last_errors = [
                    f"ARC4421 TEST_MODEL_FAILED: {requirement_id}: {describe_model_error(exc)}"
                ]
                feedback = last_errors
                self._trace("MODEL_ERROR " + "; ".join(last_errors))
                continue
            self._trace_json(
                "MODEL_OUTPUT",
                requirement_id,
                decision,
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
            if isinstance(decision, dict):
                last_decision = copy.deepcopy(decision)
            local_errors = _validate_test_decision(decision, validation_context)
            if local_errors:
                last_errors = local_errors
                feedback = local_errors
                self._trace("MODEL_REJECTED " + "; ".join(local_errors))
                continue
            sources = _decision_sources(decision, validation_context)
            self._remove_requirement_files(planned_paths)
            self._artifact_store.write_generated_tests(sources)
            has_vitest = any(
                path.startswith(("tests/unit/", "tests/integration/"))
                for path in sources
            )
            has_e2e = any(path.startswith("tests/e2e/") for path in sources)
            static_result = self._validator.validate(
                has_vitest=has_vitest,
                has_e2e=has_e2e,
                test_files=sorted(sources),
            )
            if static_result.source_diagnostics:
                self._trace(
                    "APPLICATION_TYPE_DIAGNOSTICS_DEFERRED_TO_TDD "
                    + "; ".join(static_result.source_diagnostics)
                )
            if static_result.ok:
                self._trace(
                    f"TESTS_ACCEPTED requirement={requirement_id} attempt={attempt + 1}"
                )
                return decision, sources, [], static_result.source_diagnostics
            last_errors = static_result.errors
            feedback = [
                "Only repair TypeScript syntax, imports, symbols, or test collection. "
                "Do not change expected behavior or weaken assertions.",
                *static_result.errors,
            ]
            self._trace("STATIC_VALIDATION_REJECTED " + "; ".join(static_result.errors))
        self._remove_requirement_files(planned_paths)
        return None, {}, last_errors, []

    def _remove_requirement_files(self, paths: set[str]) -> None:
        for relative in paths:
            target = (self._output_root / relative).resolve()
            if self._output_root in target.parents and target.is_file():
                target.unlink()

    def _trace(self, message: str) -> None:
        if self._trace_enabled:
            self._log.info(message)

    def _trace_json(
        self,
        marker: str,
        requirement_id: str,
        payload: Any,
        duration_ms: int | None = None,
    ) -> None:
        suffix = f" duration_ms={duration_ms}" if duration_ms is not None else ""
        self._trace(
            f"{marker} requirement={requirement_id}{suffix}\n"
            + format_payload_trace(payload)
        )


def _build_context_pack(
    *,
    output_root: Path,
    requirement_id: str,
    requirement: dict[str, Any],
    database_schema: dict[str, Any],
    design_ir: dict[str, Any],
    frontend_ir: dict[str, Any],
    resolved_targets: dict[str, Any],
    required_layers: list[str],
    test_obligations: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:

    all_target_rows = [
        _project_test_target(row)
        for row in [
            *resolved_targets.get("owned_targets", []),
            *resolved_targets.get("dependency_targets", []),
        ]
        if isinstance(row, dict)
    ]
    owned_targets = [
        _project_test_target(row)
        for row in resolved_targets.get("owned_targets", [])
        if isinstance(row, dict) and _target_relevant_to_layers(row, required_layers)
    ]
    relevant_frontend_subgraph = _project_frontend_subgraph(
        requirement_id=requirement_id,
        frontend_ir=frontend_ir,
        owned_targets=owned_targets,
        required_layers=required_layers,
    )
    one_hop_dependencies = _project_one_hop_dependencies(
        owned_targets=owned_targets,
        all_target_rows=all_target_rows,
        frontend_subgraph=relevant_frontend_subgraph,
        required_layers=required_layers,
    )
    target_rows = [*owned_targets, *one_hop_dependencies]
    referenced_type_ids = _referenced_type_ids(target_rows)
    relevant_types = [
        copy.deepcopy(row)
        for row in resolved_targets.get("type_targets", [])
        if isinstance(row, dict) and str(row.get("type_id", "")) in referenced_type_ids
    ]
    output_files = {
        layer: _test_file(requirement_id, layer)
        for layer in required_layers
    }
    public_seams: dict[str, Any] = {}
    allowed_imports: dict[str, list[dict[str, Any]]] = {}
    for layer in required_layers:
        test_file = output_files[layer]
        cards = _layer_source_cards(layer, target_rows, test_file, output_root)
        public_seams[layer] = cards
        runtime_import = _relative_import(test_file, "tests/support/runtime.ts")
        imports = [
            {
                "specifier": "vitest" if layer != "E2E" else _relative_import(test_file, "tests/support/e2e.ts"),
                "symbols": ["describe", "expect", "test"] if layer != "E2E" else ["expect", "test"],
            },
            {
                "specifier": runtime_import,
                "symbols": ["uniqueValue"],
                "signatures": {"uniqueValue": "uniqueValue(prefix: string): string"},
            },
        ]
        if requirement.get("seed_fixtures") and layer in {"INTEGRATION", "E2E"}:
            imports.append(
                {
                    "specifier": _relative_import(test_file, "tests/support/seed.ts"),
                    "symbols": ["seedRequirement"],
                    "signatures": {
                        "seedRequirement": (
                            "seedRequirement(requirementId: string, "
                            "apply?: (requirementId: string) => Promise<void>): Promise<void>"
                        )
                    },
                }
            )
            if layer == "INTEGRATION":
                imports[0]["symbols"] = ["beforeEach", "describe", "expect", "test"]
        if layer == "INTEGRATION":
            imports.extend(
                [
                    {"specifier": "supertest", "symbols": ["default"]},
                    {
                        "specifier": _relative_import(test_file, "backend/src/app.ts"),
                        "symbols": ["app"],
                    },
                ]
            )
        if layer == "UNIT":
            imports.extend(
                {
                    "specifier": card["import_specifier"],
                    "symbols": [card["symbol"]],
                }
                for card in cards
                if card.get("kind") == "FUNC"
            )
            shared_types = sorted(
                {
                    str(reference.get("symbol", ""))
                    for card in cards
                    for reference in (card.get("input_type"), card.get("output_type"))
                    if isinstance(reference, dict) and reference.get("symbol")
                }
            )
            if shared_types:
                imports.append({"specifier": "@arc/shared", "symbols": shared_types})
        allowed_imports[layer] = imports

    model_requirement = {
        key: copy.deepcopy(requirement.get(key))
        for key in (
            "id",
            "name",
            "description",
            "scenarios",
            "examples",
            "dependencies",
            "seed_fixtures",
        )
        if key in requirement
    }
    database_tables = _database_source_cards(
        output_root=output_root,
        database_schema=database_schema,
        requirement_id=requirement_id,
        resolved_targets=resolved_targets,
    )
    model_layers = _build_model_layers(
        output_root=output_root,
        requirement_id=requirement_id,
        required_layers=required_layers,
        output_files=output_files,
        public_seams=public_seams,
        allowed_imports=allowed_imports,
        relevant_types=relevant_types,
        frontend_subgraph=relevant_frontend_subgraph,
        test_obligations=test_obligations,
    )
    model_context = {
        "requirement": model_requirement,
        "database_tables": database_tables,
        "layers": model_layers,
    }
    validation_context = {
        "requirement_id": requirement_id,
        "requirement": {
            key: copy.deepcopy(requirement.get(key))
            for key in ("id", "name", "description", "scenarios", "dependencies", "seed_fixtures")
        },
        "relevant_frontend_subgraph": relevant_frontend_subgraph,
        "e2e_entry_routes": _e2e_entry_routes(requirement_id, relevant_frontend_subgraph),
        "public_seams": public_seams,
        "required_layers": required_layers,
        "output_files": output_files,
        "allowed_imports": allowed_imports,
    }
    return model_context, validation_context


def _database_source_cards(
    *, output_root: Path, database_schema: dict[str, Any],
    requirement_id: str, resolved_targets: dict[str, Any],
) -> list[dict[str, Any]]:
    schema_slice = schema_for_requirement(database_schema, requirement_id)
    paths = {
        str(row.get("type_id", "")): str(row.get("file", ""))
        for row in resolved_targets.get("type_targets", []) if isinstance(row, dict)
    }
    cards: list[dict[str, Any]] = []
    for entity in schema_slice.get("entities", []):
        table = str(entity.get("key", entity.get("name", "")))
        relative = paths.get(f"ENTITY.{table}", f"backend/src/db/schema/{table}.ts")
        path = output_root / relative
        if path.is_file():
            cards.append({"path": relative, "source": path.read_text(encoding="utf-8")})
    return cards
def _build_model_layers(
    *, output_root: Path, requirement_id: str, required_layers: list[str],
    output_files: dict[str, str], public_seams: dict[str, Any],
    allowed_imports: dict[str, list[dict[str, Any]]],
    relevant_types: list[dict[str, Any]],
    frontend_subgraph: dict[str, list[dict[str, Any]]],
    test_obligations: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    layers: dict[str, dict[str, Any]] = {}
    for layer in required_layers:
        sources = []
        seen: set[str] = set()
        for row in public_seams.get(layer, []):
            path = str(row.get("source_file", ""))
            if path and path not in seen:
                sources.append({"path": path, "source": row.get("source", "")})
                seen.add(path)
        for relative in ("tests/support/runtime.ts", "tests/support/seed.ts", "tests/support/e2e.ts"):
            path = output_root / relative
            if path.is_file() and relative not in seen:
                sources.append({"path": relative, "source": path.read_text(encoding="utf-8")})
        current: dict[str, Any] = {
            "output_file": output_files[layer],
            "imports": allowed_imports.get(layer, []),
            "sources": sources,
        }
        if layer == "E2E":
            current["entry_routes"] = _e2e_entry_routes(requirement_id, frontend_subgraph)
        layers[layer] = current
    return layers
def _target_relevant_to_layers(
    target: dict[str, Any], required_layers: list[str]
) -> bool:
    kind = str(target.get("kind", "")).upper()
    allowed = {
        "UNIT": {"FUNC", "DB"},
        "INTEGRATION": {"API", "FUNC", "DB"},
        "E2E": {"PAGE", "COMPONENT", "LAYOUT", "STORE", "API", "API_CLIENT"},
    }
    return any(kind in allowed[layer] for layer in required_layers)


def _project_test_target(row: dict[str, Any]) -> dict[str, Any]:
    """Keep only the binding facts test generation can actually use."""

    return {
        key: copy.deepcopy(row.get(key))
        for key in (
            "module_id",
            "source_ir_id",
            "kind",
            "file",
            "symbol",
            "public_signature",
            "input_type",
            "output_type",
            "props_type",
            "route",
            "callees",
            "store_types",
        )
        if key in row
    }


def _project_one_hop_dependencies(
    *,
    owned_targets: list[dict[str, Any]],
    all_target_rows: list[dict[str, Any]],
    frontend_subgraph: dict[str, list[dict[str, Any]]],
    required_layers: list[str],
) -> list[dict[str, Any]]:
    owned_ids = {
        str(row.get("module_id", "")) for row in owned_targets if row.get("module_id")
    }
    dependency_ids = {
        str(value)
        for row in owned_targets
        for value in row.get("callees", [])
        if str(value)
    }
    if "E2E" in required_layers:
        api_ids = {
            str(row.get("api_id", ""))
            for table in ("journeys", "api_usages")
            for row in frontend_subgraph.get(table, [])
            if isinstance(row, dict) and str(row.get("api_id", ""))
        }
        api_ids.update(
            str(value)
            for row in frontend_subgraph.get("screens", [])
            if isinstance(row, dict)
            for value in row.get("required_api_ids", [])
            if str(value)
        )
        dependency_ids.update(api_ids)
        dependency_ids.update(f"API_CLIENT::{api_id}" for api_id in api_ids)

    return sorted(
        [
            copy.deepcopy(row)
            for row in all_target_rows
            if str(row.get("module_id", "")) in dependency_ids - owned_ids
            and _target_relevant_to_layers(row, required_layers)
        ],
        key=lambda row: str(row.get("module_id", "")),
    )


def _project_frontend_subgraph(
    *,
    requirement_id: str,
    frontend_ir: dict[str, Any],
    owned_targets: list[dict[str, Any]],
    required_layers: list[str],
) -> dict[str, list[dict[str, Any]]]:
    owned_ids = {
        str(row.get("source_ir_id", row.get("module_id", "")))
        for row in owned_targets
    }
    screens = [
        {**_project_frontend_row(row, "screen"), "entry_candidate": True}
        for row in frontend_ir.get("screens", [])
        if isinstance(row, dict)
        and (
            str(row.get("id", "")) in owned_ids
            or requirement_id in {str(value) for value in row.get("requirement_ids", [])}
        )
    ]
    primary_screen_ids = {str(row.get("id", "")) for row in screens}
    route_index = {
        str(row.get("route", "")): row
        for row in frontend_ir.get("screens", [])
        if isinstance(row, dict) and str(row.get("route", ""))
    }
    navigation_routes = {
        str(target.get("target_route", ""))
        for screen in screens
        for target in screen.get("navigation_targets", [])
        if isinstance(target, dict) and str(target.get("target_route", ""))
    }
    screen_ids = {str(row.get("id", "")) for row in screens}
    for route in sorted(navigation_routes):
        destination = route_index.get(route)
        destination_id = str((destination or {}).get("id", ""))
        if destination is not None and destination_id not in screen_ids:
            screens.append(_project_frontend_row(destination, "screen"))
            screen_ids.add(destination_id)

    journeys = [
        _project_frontend_row(row, "journey")
        for row in frontend_ir.get("journeys", [])
        if isinstance(row, dict)
        and (
            str(row.get("requirement_id", "")) == requirement_id
            or str(row.get("source_screen_id", "")) in primary_screen_ids
        )
    ]
    api_ids = {
        str(row.get("api_id", ""))
        for row in journeys
        if str(row.get("api_id", ""))
    }
    api_ids.update(
        str(value)
        for row in screens
        for value in row.get("required_api_ids", [])
        if str(value)
    )
    api_usages = [
        _project_frontend_row(row, "api_usage")
        for row in frontend_ir.get("api_usages", [])
        if isinstance(row, dict)
        and str(row.get("screen_id", row.get("consumer_id", ""))) in screen_ids
    ]
    shared_state_policies = [
        _project_frontend_row(row, "state")
        for row in frontend_ir.get("shared_state_policies", [])
        if isinstance(row, dict)
        and (
            str(row.get("id", "")) in owned_ids
            or requirement_id in {str(value) for value in row.get("requirement_ids", [])}
            or str(row.get("requirement_id", "")) == requirement_id
        )
    ]
    screen_components = [
        _project_frontend_row(row, "component")
        for row in frontend_ir.get("screen_components", [])
        if isinstance(row, dict) and str(row.get("screen_id", "")) in screen_ids
    ]
    if "E2E" not in required_layers:
        return {"screens": [], "screen_components": [], "journeys": [], "api_usages": [], "shared_state_policies": []}
    return {
        "screens": screens,
        "screen_components": screen_components,
        "journeys": journeys,
        "api_usages": api_usages,
        "shared_state_policies": shared_state_policies,
    }


def _e2e_entry_routes(
    requirement_id: str,
    frontend_subgraph: dict[str, list[dict[str, Any]]],
) -> list[str]:
    screens = {
        str(row.get("id", "")): row
        for row in frontend_subgraph.get("screens", [])
        if isinstance(row, dict) and row.get("id")
    }
    journey_routes = {
        str(screens.get(str(row.get("source_screen_id", "")), {}).get("route", ""))
        for row in frontend_subgraph.get("journeys", [])
        if isinstance(row, dict) and str(row.get("requirement_id", "")) == requirement_id
    }
    if any(journey_routes):
        return sorted(route for route in journey_routes if route)
    return sorted({
        str(row.get("route", ""))
        for row in screens.values()
        if row.get("entry_candidate") and row.get("route")
    })


def _project_frontend_row(row: dict[str, Any], kind: str) -> dict[str, Any]:
    fields = {
        "screen": ("id", "route", "title", "description", "requirement_ids", "required_api_ids", "navigation_targets", "visual_reference_ids"),
        "component": ("id", "screen_id", "purpose", "requirement_ids", "required_api_ids", "observable_states", "visual_reference_ids"),
        "journey": ("id", "requirement_id", "source_screen_id", "target_screen_id", "steps", "api_id"),
        "api_usage": ("screen_id", "consumer_id", "api_id", "purpose", "trigger"),
        "state": ("id", "name", "requirement_ids", "persistence", "storage_key", "state", "actions"),
    }[kind]
    return {key: copy.deepcopy(row.get(key)) for key in fields if key in row}


def _referenced_type_ids(targets: list[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for target in targets:
        for key in ("input_type", "output_type", "props_type"):
            reference = target.get(key)
            if isinstance(reference, dict) and str(reference.get("type_id", "")):
                result.add(str(reference["type_id"]))
        for reference in target.get("store_types", []):
            if isinstance(reference, dict) and str(reference.get("type_id", "")):
                result.add(str(reference["type_id"]))
    return result


def _context_audit(
    requirement_id: str, attempt: int, payload: dict[str, Any]
) -> str:
    def size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    envelope = {
        "instructions": TEST_GENERATION_INSTRUCTIONS,
        "input_payload": payload,
        "output_schema": TEST_GENERATION_SCHEMA,
    }
    fields = {
        "context_total_chars": size(envelope),
        "instructions_chars": size(TEST_GENERATION_INSTRUCTIONS),
        "input_payload_chars": size(payload),
        "output_schema_chars": size(TEST_GENERATION_SCHEMA),
        "requirement_chars": size(payload.get("requirement", {})),
        "requirement_contract_chars": size(payload.get("requirement_contract", {})),
        "database_tables_chars": size(payload.get("database_tables", [])),
        "layers_chars": size(payload.get("layers", {})),
        "target_modules_chars": size(payload.get("target_modules", {})),
        "seed_chars": size(payload.get("seed", {})),
    }
    return (
        f"CONTEXT_AUDIT phase=test_generation requirement={requirement_id} "
        f"attempt={attempt} "
        + " ".join(f"{key}={value}" for key, value in fields.items())
    )


def _plan_test_obligations(
    requirement_id: str,
    resolved_targets: dict[str, Any],
    design_ir: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    targets = [
        row
        for row in [
            *resolved_targets.get("owned_targets", []),
            *resolved_targets.get("dependency_targets", []),
        ]
        if isinstance(row, dict)
    ]
    owned = [
        row for row in resolved_targets.get("owned_targets", []) if isinstance(row, dict)
    ]
    kinds = {str(row.get("kind", "")) for row in owned}
    module_index = {
        str(row.get("id", "")): row
        for row in design_ir.get("modules", [])
        if isinstance(row, dict)
    }
    obligations: dict[str, dict[str, Any]] = {}
    ui_modules = sorted(
        str(row.get("module_id", ""))
        for row in owned
        if row.get("kind") in {"PAGE", "COMPONENT", "LAYOUT"}
        and str(row.get("module_id", ""))
    )
    if ui_modules:
        obligations["E2E"] = {
            "reason": "requirement owns a browser-observable Page/Component/Layout path",
            "target_modules": ui_modules,
        }

    api_ids = [str(row.get("source_ir_id", "")) for row in owned if row.get("kind") == "API"]
    db_ids = [str(row.get("source_ir_id", "")) for row in targets if row.get("kind") == "DB"]
    write_operation = any(
        str(effect.get("operation", "")).upper() in {"CREATE", "UPDATE", "DELETE", "WRITE"}
        for module_id in {*api_ids, *db_ids}
        for effect in module_index.get(module_id, {}).get("effects", [])
        if isinstance(effect, dict)
    )
    if api_ids and (db_ids or write_operation):
        obligations["INTEGRATION"] = {
            "reason": "requirement owns an HTTP API connected to database or persistent effects",
            "target_modules": sorted(
                str(row.get("module_id", ""))
                for row in owned
                if row.get("kind") == "API" and str(row.get("module_id", ""))
            ),
        }

    func_modules = [
        module_index.get(str(row.get("source_ir_id", "")), {})
        for row in owned
        if row.get("kind") == "FUNC"
    ]
    pure_cache: dict[str, bool] = {}

    def is_unit_seam(module_id: str, visiting: set[str] | None = None) -> bool:
        """Accept callable FUNC closures with no declared or delegated side effects."""

        if module_id in pure_cache:
            return pure_cache[module_id]
        module = module_index.get(module_id, {})
        if str(module.get("kind", "")).upper() != "FUNC":
            pure_cache[module_id] = False
            return False
        if any(isinstance(effect, dict) for effect in module.get("effects", [])):
            pure_cache[module_id] = False
            return False
        active = set(visiting or ())
        if module_id in active:
            pure_cache[module_id] = False
            return False
        active.add(module_id)
        for callee_id in module.get("callees", []):
            callee = str(callee_id)
            if not callee or not is_unit_seam(callee, active):
                pure_cache[module_id] = False
                return False
        pure_cache[module_id] = True
        return True

    unit_func_ids = sorted(
        str(module.get("id", ""))
        for module in func_modules
        if str(module.get("id", ""))
        and is_unit_seam(str(module.get("id", "")))
    )
    if unit_func_ids:
        obligations["UNIT"] = {
            "reason": (
                "requirement owns independently callable FUNC modules whose transitive "
                "dependency closure has no declared side effects"
            ),
            "target_modules": unit_func_ids,
        }

    if not obligations:
        if "API" in kinds:
            obligations["INTEGRATION"] = {
                "reason": "API is the narrowest available public seam",
                "target_modules": sorted(
                    str(row.get("module_id", ""))
                    for row in owned
                    if row.get("kind") == "API" and str(row.get("module_id", ""))
                ),
            }
        elif kinds & {"PAGE", "COMPONENT", "LAYOUT"}:
            obligations["E2E"] = {
                "reason": "browser UI is the available public seam",
                "target_modules": ui_modules,
            }
    return {
        layer: obligations[layer]
        for layer in TEST_LAYERS
        if layer in obligations
    }


def _validate_test_decision(
    decision: Any,
    context_pack: dict[str, Any],
) -> list[str]:
    requirement_id = str(context_pack["requirement_id"])
    if not isinstance(decision, dict) or set(decision) != {"files"}:
        return [f"ARC4422 TEST_OUTPUT_INVALID: {requirement_id} must return only files."]
    files = decision["files"]
    if not isinstance(files, list):
        return [f"ARC4422 TEST_OUTPUT_INVALID: {requirement_id} files must be a list."]
    expected = set(context_pack["required_layers"])
    actual: list[str] = []
    errors: list[str] = []
    for row in files:
        if not isinstance(row, dict) or set(row) != {"layer", "code"}:
            errors.append("ARC4422 TEST_OUTPUT_INVALID: each file needs layer and code.")
            continue
        layer, code = str(row["layer"]).upper(), row["code"]
        actual.append(layer)
        if layer not in expected or not isinstance(code, str) or not code.strip():
            errors.append(f"ARC4423 TEST_LAYER_INVALID: {layer} is unavailable or empty.")
            continue
        errors.extend(_validate_test_code(layer, code, context_pack))
    if len(actual) != len(set(actual)) or set(actual) != expected:
        errors.append(f"ARC4423 TEST_LAYER_INVALID: expected {sorted(expected)}, received {actual}.")
    return list(dict.fromkeys(errors))
def _validate_test_code(
    layer: str,
    code: str,
    context_pack: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if "```" in code:
        errors.append(f"ARC4426 TEST_CODE_INVALID: {layer} contains markdown fences.")
    if re.search(r"\b(?:test|it|describe)\.(?:only|skip)\b", code):
        errors.append(f"ARC4426 TEST_CODE_INVALID: {layer} contains focused or skipped tests.")
    if re.search(r"\b(?:test|it|describe)\.concurrent\b", code):
        errors.append(
            f"ARC4428 TEST_ISOLATION_INVALID: {layer} must not run generated tests concurrently."
        )
    if layer in {"INTEGRATION", "E2E"}:
        if re.search(r"\b(?:beforeAll|afterAll|test\.beforeAll|test\.afterAll)\s*\(", code):
            errors.append(
                f"ARC4428 TEST_ISOLATION_INVALID: {layer} must not share setup or teardown across tests."
            )
        if re.search(r"\b(?:test\.)?describe\.configure\s*\(\s*\{[^}]*\bmode\s*:\s*['\"]parallel['\"]", code, re.DOTALL):
            errors.append(
                f"ARC4428 TEST_ISOLATION_INVALID: {layer} must not override sequential execution."
            )
    if not re.search(r"\b(?:test|it)\s*\(", code):
        errors.append(f"ARC4426 TEST_CODE_INVALID: {layer} contains no executable test declaration.")
    allowed = {
        str(row.get("specifier", ""))
        for row in context_pack["allowed_imports"].get(layer, [])
        if isinstance(row, dict)
    }
    imports = re.findall(r"\bfrom\s+[\"']([^\"']+)[\"']", code)
    imports.extend(
        re.findall(r"\bimport\s+(?:type\s+)?[\"']([^\"']+)[\"']", code)
    )
    imports.extend(
        re.findall(
            r"\b(?:import|require)\s*\(\s*[\"']([^\"']+)[\"']\s*\)",
            code,
        )
    )
    invalid = sorted(
        specifier
        for specifier in imports
        if specifier not in allowed
    )
    if invalid:
        errors.append(
            f"ARC4427 TEST_IMPORT_INVALID: {layer} imports unavailable specifiers {invalid}."
        )
    required_package = (
        _relative_import(context_pack["output_files"][layer], "tests/support/e2e.ts")
        if layer == "E2E" else "vitest"
    )
    if required_package not in imports:
        errors.append(
            f"ARC4427 TEST_IMPORT_INVALID: {layer} must import {required_package}."
        )
    if layer == "E2E":
        runtime_import = required_package
        named_imports = re.findall(
            r"\bimport\s*\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]",
            code,
            flags=re.DOTALL,
        )
        if not any(
            specifier == runtime_import
            and re.search(r"(?:^|,)\s*test\s*(?:,|$)", symbols)
            for symbols, specifier in named_imports
        ):
            errors.append(
                "ARC4428 TEST_ISOLATION_INVALID: E2E must import test from the "
                "compiler-owned support/e2e module."
            )
        if re.search(r"\bresetE2EState\s*\(", code):
            errors.append(
                "ARC4428 TEST_ISOLATION_INVALID: E2E reset is automatic; "
                "do not reset again from generated test source."
            )
    if re.search(r"\buniqueValue\s*\(\s*\)", code):
        errors.append(
            f"ARC4430 TEST_HELPER_SIGNATURE_INVALID: {layer} must pass a prefix to uniqueValue."
        )
    if re.search(r"\bseedRequirement\s*\(\s*\)", code):
        errors.append(
            f"ARC4430 TEST_HELPER_SIGNATURE_INVALID: {layer} must pass requirement_id to seedRequirement."
        )
    if layer == "E2E":
        allowed_routes = {
            str(row.get("route", "")).strip()
            for row in context_pack.get("relevant_frontend_subgraph", {}).get("screens", [])
            if isinstance(row, dict) and str(row.get("route", "")).strip()
        }
        entry_routes = {
            str(value)
            for value in context_pack.get("e2e_entry_routes", [])
            if str(value)
        }
        declared_entries = re.findall(
            r"\b(?:entryRoute|baseRoute|startRoute)\s*=\s*[\"']([^\"']+)[\"']",
            code,
        )
        invalid_entries = sorted({
            route
            for route in declared_entries
            if entry_routes and not _route_is_allowed(route, entry_routes)
        })
        if invalid_entries:
            errors.append(
                "ARC4429 E2E_ENTRY_ROUTE_INVALID: starting routes must come from "
                f"this requirement's source screens: {invalid_entries}; entries={sorted(entry_routes)}."
            )
        literal_routes = re.findall(
            r"\bpage\.goto\s*\(\s*[\"']([^\"']+)[\"']\s*\)",
            code,
        )
        if entry_routes and not declared_entries and literal_routes:
            first_route = literal_routes[0]
            if first_route.startswith("/") and not _route_is_allowed(first_route, entry_routes):
                errors.append(
                    "ARC4429 E2E_ENTRY_ROUTE_INVALID: the first direct navigation "
                    f"must start at a requirement source screen: {first_route}; entries={sorted(entry_routes)}."
                )
        literal_routes.extend(
            re.findall(
                r"\b(?:entryRoute|baseRoute|startRoute)\s*=\s*[\"']([^\"']+)[\"']",
                code,
            )
        )
        route_constants = {
            name: route
            for name, route in re.findall(
                r"\b(?:const|let)\s+(\w+)\s*=\s*[\"'](/[^\"']*)[\"']",
                code,
            )
        }
        literal_routes.extend(
            route_constants[name]
            for name in re.findall(r"\bpage\.goto\s*\(\s*(\w+)\s*\)", code)
            if name in route_constants
        )
        invalid_routes = sorted(
            {
                route
                for route in literal_routes
                if route.startswith("/")
                and not _route_is_allowed(route, allowed_routes)
            }
        )
        if invalid_routes:
            errors.append(
                "ARC4429 E2E_ROUTE_INVALID: E2E uses routes not present in the "
                f"frontend screen graph: {invalid_routes}; allowed={sorted(allowed_routes)}."
            )
        source_corpus = "\n".join(
            str(row.get("source", ""))
            for row in context_pack.get("public_seams", {}).get("E2E", [])
            if isinstance(row, dict) and str(row.get("source", ""))
        )
        if source_corpus:
            # Lowering keeps semantic obligations in scaffolding attributes.
            # Those descriptions must not authorize literal UI-copy assertions.
            visible_source = re.sub(
                r"<span\b[^>]*\bdata-arc-obligation=\{[^}]*\}[^>]*>.*?</span>",
                "",
                source_corpus,
                flags=re.DOTALL,
            )
            visible_source = re.sub(r"/\*.*?\*/|^[ \t]*//[^\n]*", "", visible_source, flags=re.DOTALL | re.MULTILINE)
            normalized_source = _normalize_ui_text(visible_source)
            locator_literals = re.findall(
                r"\bgetBy(?:Text|Label|Placeholder)\s*\(\s*[\"']([^\"']+)[\"']",
                code,
            )
            locator_literals.extend(
                re.findall(
                    r"\bgetByRole\s*\(\s*[\"'][^\"']+[\"']\s*,\s*"
                    r"\{[^{}]*\bname\s*:\s*[\"']([^\"']+)[\"']",
                    code,
                    flags=re.DOTALL,
                )
            )
            invented_locators = sorted(
                {
                    value
                    for value in locator_literals
                    if _normalize_ui_text(value) != "implementation pending"
                    if _normalize_ui_text(value) not in normalized_source
                }
            )
            if any(_normalize_ui_text(value) == "implementation pending" for value in locator_literals):
                invented_locators.append("Implementation pending")
            if invented_locators:
                errors.append(
                    "ARC4432 E2E_LOCATOR_INVALID: literal locator text must exist in "
                    f"the supplied frontend source: {invented_locators}."
                )
    if layer == "INTEGRATION" and (
        "supertest" not in imports
        or _relative_import(context_pack["output_files"][layer], "backend/src/app.ts")
        not in imports
    ):
        errors.append(
            "ARC4427 TEST_IMPORT_INVALID: INTEGRATION must use Supertest and the exported app."
        )
    seed_fixtures = context_pack.get("requirement", {}).get("seed_fixtures", [])
    if seed_fixtures and layer in {"INTEGRATION", "E2E"}:
        seed_import = _relative_import(
            context_pack["output_files"][layer], "tests/support/seed.ts"
        )
        if seed_import not in imports or not re.search(r"\bseedRequirement\s*\(", code):
            errors.append(
                f"ARC4428 TEST_SEED_INVALID: {layer} must use the compiler-owned seedRequirement helper."
            )
        if not re.search(r"\b(?:beforeEach|test\.beforeEach)\s*\(", code):
            errors.append(
                f"ARC4428 TEST_SEED_INVALID: {layer} must apply fixtures in beforeEach."
            )
    return errors


def _normalize_ui_text(value: str) -> str:
    """Normalize source and locator literals for deterministic copy validation."""

    return " ".join(str(value).casefold().split())


def _route_is_allowed(route: str, allowed_routes: set[str]) -> bool:
    """Match a concrete E2E URL path against static and `:parameter` routes."""

    path = str(route).split("?", 1)[0].split("#", 1)[0]
    for allowed in allowed_routes:
        pattern = re.sub(r":\w+", r"[^/]+", re.escape(allowed))
        pattern = pattern.replace(r"\*", ".*")
        if re.fullmatch(pattern, path):
            return True
    return False


def _decision_sources(
    decision: dict[str, Any], context_pack: dict[str, Any]
) -> dict[str, str]:
    return {
        str(context_pack["output_files"][str(row["layer"]).upper()]):
        str(row["code"]).rstrip() + "\n"
        for row in decision.get("files", [])
        if isinstance(row, dict)
    }


def _manifest_rows(
    requirement_id: str,
    requirement: dict[str, Any],
    decision: dict[str, Any],
    context_pack: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    files = []
    for row in decision["files"]:
        layer = str(row["layer"]).upper()
        files.append({
            "requirement_id": requirement_id,
            "layer": layer,
            "test_file": str(context_pack["output_files"][layer]),
            "content_sha256": hashlib.sha256((row["code"].rstrip() + "\n").encode("utf-8")).hexdigest(),
        })
    return [], files
def _layer_source_cards(
    layer: str,
    targets: list[dict[str, Any]],
    test_file: str,
    output_root: Path,
) -> list[dict[str, Any]]:
    allowed_kinds = {
        "UNIT": {"FUNC"},
        "INTEGRATION": {"API", "FUNC", "DB"},
        "E2E": {"PAGE", "COMPONENT", "LAYOUT", "API", "API_CLIENT"},
    }[layer]
    rows: list[dict[str, Any]] = []
    for target in targets:
        if str(target.get("kind", "")) not in allowed_kinds:
            continue
        row = {
            key: copy.deepcopy(target.get(key))
            for key in (
                "module_id",
                "source_ir_id",
                "kind",
                "file",
                "symbol",
                "public_signature",
                "input_type",
                "output_type",
                "props_type",
                "route",
                "callees",
            )
        }
        source_file = str(target.get("file", ""))
        row["source_file"] = source_file
        try:
            row["source"] = (output_root / source_file).read_text(encoding="utf-8")
        except OSError:
            row["source"] = ""
        row["import_specifier"] = _relative_import(test_file, str(target.get("file", "")))
        rows.append(row)
    return sorted(rows, key=lambda row: str(row.get("module_id", "")))


def _test_file(requirement_id: str, layer: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", requirement_id.lower()).strip("-") or "requirement"
    digest = hashlib.sha256(requirement_id.encode("utf-8")).hexdigest()[:8]
    return f"tests/{layer.lower()}/{stem}-{digest}.spec.ts"


def _relative_import(test_file: str, source_file: str) -> str:
    relative = posixpath.relpath(source_file, posixpath.dirname(test_file))
    if not relative.startswith("."):
        relative = f"./{relative}"
    return re.sub(r"\.(?:ts|tsx)$", ".js", relative)


def _atomic_order(
    requirement_ir: dict[str, Any], dependency_graph: dict[str, Any]
) -> list[str]:
    atomic_ids = {
        str(value) for value in requirement_ir.get("atomic_units", []) if str(value)
    }
    result: list[str] = []
    for wave in dependency_graph.get("atomic_implementation_waves", []):
        if not isinstance(wave, list):
            continue
        result.extend(
            value
            for value in sorted({str(item) for item in wave})
            if value in atomic_ids and value not in result
        )
    result.extend(sorted(atomic_ids - set(result)))
    return result


def _empty_test_manifest(status: str) -> dict[str, Any]:
    return {"status": status, "files": []}
def _test_generation_precondition_errors(
    *,
    requirement_id: str,
    requirement_ir: dict[str, Any],
    code_binding_registry: dict[str, Any],
    environment_manifest: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if code_binding_registry.get("status") != CODE_BINDING_READY:
        errors.append(
            "ARC4410 CODE_BINDING_NOT_READY: Test Generation requires CODE_BINDING_READY."
        )
    if environment_manifest.get("status") != TEST_ENVIRONMENT_READY:
        errors.append(
            "ARC4411 TEST_ENVIRONMENT_NOT_READY: Test environment is unavailable."
        )
    nodes = requirement_ir.get("nodes", {})
    atomic_ids = {
        str(value) for value in requirement_ir.get("atomic_units", []) if str(value)
    }
    if not requirement_id:
        errors.append("ARC4412 TEST_CONTEXT_INVALID: requirement_id is required.")
    elif not isinstance(nodes, dict) or not isinstance(nodes.get(requirement_id), dict):
        errors.append(
            f"ARC4412 TEST_CONTEXT_INVALID: missing atomic requirement {requirement_id}."
        )
    elif requirement_id not in atomic_ids:
        errors.append(
            f"ARC4412 TEST_CONTEXT_INVALID: {requirement_id} is not an atomic requirement."
        )
    return errors


def _replace_requirement_slice(
    manifest: dict[str, Any], *, requirement_id: str, state: str,
    test_rows: list[dict[str, Any]], file_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": state,
        "files": sorted(
            [row for row in manifest.get("files", []) if row.get("requirement_id") != requirement_id]
            + file_rows,
            key=lambda row: row["test_file"],
        ),
    }


def _manifest_requirement_order(manifest: dict[str, Any]) -> list[str]:
    return sorted({str(row["requirement_id"]) for row in manifest.get("files", [])})


def _finalize_manifest(
    manifest: dict[str, Any], *, status: str, requirement_order: list[str],
    node_states: dict[str, str], environment_manifest: dict[str, Any],
    code_binding_registry: dict[str, Any],
) -> dict[str, Any]:
    return {"status": status, "files": manifest.get("files", [])}
def _test_workspace_path(test_file: str) -> str:
    normalized = str(test_file).replace("\\", "/").strip().strip("/")
    return normalized.removeprefix("tests/")


def _command_output(stdout: str | None, stderr: str | None) -> str:
    value = "\n".join(part.strip() for part in (stdout or "", stderr or "") if part.strip())
    return value[-6000:] if value else "no command output"


def _application_only_type_errors(output: str) -> list[str]:
    """Return source-only TS errors; never hide test or unlocated diagnostics."""

    error_lines = [
        line.strip() for line in output.splitlines()
        if re.search(r"\berror TS\d+:", line)
    ]
    if not error_lines:
        return []
    paths = [
        re.match(r"^(.+?\.tsx?)\(\d+,\d+\): error TS\d+:", line)
        for line in error_lines
    ]
    if any(match is None for match in paths):
        return []
    for match in paths:
        assert match is not None
        path = match.group(1).replace("\\", "/").lstrip("./")
        if not any(
            path.startswith(f"{root}/src/") or f"/{root}/src/" in path
            for root in ("backend", "frontend", "shared")
        ):
            return []
    return error_lines


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return payload


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(os.environ.get(name, str(default))), maximum))
    except ValueError:
        return default


def _bounded_float_env(
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


def _env_flag(environment: Mapping[str, str], name: str, default: bool) -> bool:
    value = environment.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


__all__ = [
    "RequirementTestGenerationPass",
    "TESTS_FROZEN",
    "TEST_ENVIRONMENT_READY",
    "TestEnvironmentInitializer",
    "TestEnvironmentResult",
    "TestGenerationResult",
    "TestStaticValidator",
]
