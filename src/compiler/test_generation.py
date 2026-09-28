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

from arcbench_agent_runtime.jsonio import write_json_atomic
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
code. E2E controls may not exist in the unimplemented skeleton yet: choose accessible
locators from the requirement, not from placeholder text. E2E may navigate directly
to any route in the supplied frontend screen graph; entry routes are suggestions.
Do not use compiler-only data-arc-page, data-arc-component, data-arc-layout,
or data-arc-obligation attributes as E2E locators; the implemented UI removes them.
When selecting form controls by a literal label, use getByLabel("label", { exact: true })
to avoid substring matches such as "Name" matching "Username"; anchored regular
expressions are also acceptable. Apply the same care to accessible role names.
For E2E helper function parameters, Playwright types can be imported from
@playwright/test with import type or inline import("@playwright/test").Page;
runtime test and expect must come from the compiler-owned support/e2e module.
For integration tests, import a default client from "supertest" and call it with
process.env.ARC_TEST_BASE_URL! (or a local variable assigned from that value).
For example: import request from "supertest"; const api = request(process.env.ARC_TEST_BASE_URL!);
then send requests with api.get("/...") or api.post("/..."). Do not use fetch instead of Supertest.
Send real requests to the separately started backend.
api_contracts is authoritative for HTTP method, path and input_source. For a GET
query contract use api.get(path).query(input); for a body contract use its declared
method and .send(input). Never infer a POST from the presence of a request DTO.
Reuse the declared prerequisite APIs and do not invent test-only endpoints.
The test runner resets the database before every
integration and E2E test. Prepare test-specific data through public API requests
or test inputs, not application fixture declarations or /__arc/seed. Never import
the Express app or backend modules directly in an integration test. Assert status, body, cookies,
and observable persistence via API requests where the requirement calls for them.
Generate exactly one complete TypeScript file for each supplied layer, with
real assertions over inputs, outputs and persisted data. Keep each test independent;
use fresh unique values and arrange any prerequisite records through public APIs.
Unit tests must import only the pure functions listed for the UNIT layer.
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


@dataclass(slots=True)
class TestGenerationResult:
    manifest: dict[str, Any]
    node_states: dict[str, str]
    artifacts: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

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
        # Empty test layer directories are not represented in Git commits.
        # Recreate the compiler-owned roots before validating the restored
        # checkpoint so --start-from lowered can continue into TDD.
        for directory in ("unit", "integration", "e2e", "support"):
            try:
                (self.tests_root / directory).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                errors.append(
                    f"ARC4401 TEST_ENVIRONMENT_INVALID: cannot create tests/{directory}: {exc}"
                )
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
        if not isinstance(project_test_environment, dict) or project_test_environment.get(
            "status"
        ) not in {"TEST_ENVIRONMENT_PENDING", TEST_ENVIRONMENT_READY}:
            errors.append(
                "ARC4401 TEST_ENVIRONMENT_INVALID: Project Manifest has no test environment."
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

        should_install_browser = self.environment.get("ARC_TEST_INSTALL_BROWSER", "1").strip().lower() not in {
            "0", "false", "no", "off", "",
        }
        if not errors and should_install_browser and not browser_installed:
            executable = resolve_executable("npm", self.environment)
            if executable is None:
                errors.append("ARC4401 TEST_ENVIRONMENT_INVALID: npm is unavailable.")
            else:
                try:
                    installed = run_command(
                        [executable, "exec", "-w", "@arc/tests", "--", "playwright", "install", "chromium"],
                        cwd=self.output_root, environment=self.environment, timeout=900,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    errors.append(f"ARC4401 TEST_ENVIRONMENT_INVALID: browser installation failed: {exc}")
                else:
                    if installed.returncode != 0:
                        errors.append(
                            "ARC4401 TEST_ENVIRONMENT_INVALID: browser installation failed: "
                            + (installed.stderr or installed.stdout)
                        )
                    else:
                        browser_installed = True

        if not errors:
            project_manifest["testEnvironment"]["status"] = TEST_ENVIRONMENT_READY
            project_manifest["testEnvironment"]["browserInstalled"] = browser_installed
            try:
                write_json_atomic(
                    self.output_root / ".arc" / "project" / "project-manifest.json",
                    project_manifest,
                )
            except OSError as exc:
                errors.append(f"ARC4401 TEST_ENVIRONMENT_INVALID: cannot save test environment: {exc}")

        manifest = {
            "status": TEST_ENVIRONMENT_READY if not errors else "TEST_ENVIRONMENT_FAILED",
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
                errors.append(
                    "ARC4431 TEST_STATIC_VALIDATION_FAILED: "
                    f"{command!r} exited with {completed.returncode}: "
                    f"{_command_output(completed.stdout, completed.stderr)}"
                )
                break
        return TestStaticValidationResult(
            ok=not errors,
            errors=errors,
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
    """Generate and freeze tests for atomic requirements and aggregate UI nodes."""

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
            manifest = _empty_test_manifest("TEST_GENERATION_FAILED")
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
        """Generate, validate, and freeze tests for exactly one requirement.

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
        if requirement_id in requirement_ir.get("folder_nodes", []):
            node = {
                **node,
                "children": [
                    {
                        key: child.get(key)
                        for key in ("id", "name", "description", "scenarios")
                    }
                    for child_id in node.get("children_ids", [])
                    if isinstance(child := nodes.get(child_id), dict)
                ],
            }
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
            )
            artifact = self._artifact_store.write_test_manifest(manifest)
            return TestGenerationResult(
                manifest=manifest,
                node_states=state,
                artifacts={"test_manifest": artifact},
                errors=errors,
            )

        required_layers = _plan_test_layers(resolved_targets, design_ir)
        if requirement_id in requirement_ir.get("folder_nodes", []):
            related_screens = [
                screen for screen in frontend_ir.get("components" if "root_component_id" in frontend_ir else "screens", [])
                if isinstance(screen, dict)
                and requirement_id in screen.get("requirement_ids", [])
            ]
            required_layers = ["E2E"] if related_screens or "E2E" in required_layers else []
        if not required_layers:
            state[requirement_id] = "FAILED"
            errors = [
                f"ARC4413 TEST_LAYER_UNRESOLVED: no public test seam for {requirement_id}."
            ]
            manifest = _finalize_manifest(
                base_manifest,
                status="TEST_GENERATION_FAILED",
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
            include_read_only_frontend=requirement_id in requirement_ir.get("folder_nodes", []),
        )
        artifacts: dict[str, str] = {}
        decision, sources, errors = self._generate_and_validate(
            requirement_id,
            context_pack,
            validation_context,
        )
        if decision is None:
            state[requirement_id] = "FAILED"
            manifest = _finalize_manifest(
                base_manifest,
                status="TEST_GENERATION_FAILED",
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
        file_rows = _manifest_rows(
            requirement_id,
            decision,
            validation_context,
        )
        manifest = _replace_requirement_slice(
            base_manifest,
            requirement_id=requirement_id,
            state=TESTS_FROZEN,
            file_rows=file_rows,
        )
        state[requirement_id] = TESTS_FROZEN
        artifacts["test_manifest"] = self._artifact_store.write_test_manifest(manifest)
        return TestGenerationResult(
            manifest=manifest,
            node_states=state,
            artifacts=artifacts,
        )

    def _generate_and_validate(
        self,
        requirement_id: str,
        context_pack: dict[str, Any],
        validation_context: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, str], list[str]]:
        feedback: list[str] = []
        last_errors: list[str] = []
        planned_paths = set(validation_context["output_files"].values())
        for attempt in range(self._retries + 1):
            payload = copy.deepcopy(context_pack)
            if feedback:
                payload["feedback"] = feedback
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
            if static_result.ok:
                self._trace(
                    f"TESTS_ACCEPTED requirement={requirement_id} attempt={attempt + 1}"
                )
                return decision, sources, []
            last_errors = static_result.errors
            feedback = [
                "Only repair TypeScript syntax, imports, symbols, or test collection. "
                "Do not change expected behavior or weaken assertions.",
                *static_result.errors,
            ]
            self._trace("STATIC_VALIDATION_REJECTED " + "; ".join(static_result.errors))
        self._remove_requirement_files(planned_paths)
        return None, {}, last_errors

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
    include_read_only_frontend: bool = False,
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
    context_targets = list(owned_targets)
    if include_read_only_frontend:
        context_targets.extend(
            row for row in resolved_targets.get("dependency_targets", [])
            if isinstance(row, dict)
            and row.get("kind") in {"PAGE", "COMPONENT", "LAYOUT", "STORE", "API_CLIENT"}
        )
    test_targets = [
        _project_test_target(row)
        for row in context_targets
        if isinstance(row, dict) and _target_relevant_to_layers(row, required_layers)
    ]
    relevant_frontend_subgraph = _project_frontend_subgraph(
        requirement_id=requirement_id,
        frontend_ir=frontend_ir,
        owned_targets=owned_targets,
        required_layers=required_layers,
    )
    one_hop_dependencies = _project_one_hop_dependencies(
        owned_targets=test_targets,
        all_target_rows=all_target_rows,
        frontend_subgraph=relevant_frontend_subgraph,
        required_layers=required_layers,
    )
    target_rows = [*test_targets, *one_hop_dependencies]
    referenced_type_ids = _referenced_type_ids(target_rows)
    relevant_types = [
        copy.deepcopy(row)
        for row in resolved_targets.get("type_targets", [])
        if isinstance(row, dict) and str(row.get("type_id", "")) in referenced_type_ids
    ]
    shared_type_symbols = sorted({
        str(row["symbol"])
        for row in relevant_types
        if row.get("symbol")
    })
    output_files = {
        layer: _test_file(requirement_id, layer)
        for layer in required_layers
    }
    pure_function_ids = _pure_function_ids(design_ir)
    public_seams: dict[str, Any] = {}
    allowed_imports: dict[str, list[dict[str, Any]]] = {}
    for layer in required_layers:
        test_file = output_files[layer]
        cards = _layer_source_cards(
            layer, target_rows, test_file, output_root,
            pure_function_ids=pure_function_ids,
        )
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
            },
        ]
        if layer == "INTEGRATION":
            imports[0]["symbols"] = ["beforeEach", "describe", "expect", "test"]
            imports.append({"specifier": "supertest", "symbols": ["default"]})
        if layer in {"INTEGRATION", "E2E"}:
            imports.append({
                "specifier": "@arc/shared", "symbols": shared_type_symbols,
                "type_only": True,
            })
        if layer == "E2E":
            imports.append({"specifier": "@playwright/test", "symbols": ["Page"], "type_only": True})
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
            "visual_references",
            "dependencies",
            "children",
        )
        if key in requirement
    }
    database_tables = _database_source_cards(
        output_root=output_root,
        database_schema=database_schema,
        requirement_id=requirement_id,
        resolved_targets=resolved_targets,
    )
    model_layers, source_files = _build_model_layers(
        output_root=output_root,
        requirement_id=requirement_id,
        required_layers=required_layers,
        output_files=output_files,
        public_seams=public_seams,
        allowed_imports=allowed_imports,
        relevant_types=relevant_types,
        frontend_subgraph=relevant_frontend_subgraph,
    )
    source_files.update((row["path"], row["source"]) for row in database_tables)
    model_context = {
        "requirement": model_requirement,
        "source_files": source_files,
        "layers": model_layers,
        "api_contracts": [row for row in all_target_rows if row.get("kind") == "API"],
    }
    validation_context = {
        "requirement_id": requirement_id,
        "relevant_frontend_subgraph": relevant_frontend_subgraph,
        "e2e_entry_routes": _e2e_entry_routes(requirement_id, relevant_frontend_subgraph),
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
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    layers: dict[str, dict[str, Any]] = {}
    source_files: dict[str, str] = {}
    for layer in required_layers:
        for row in public_seams.get(layer, []):
            relative = str(row.get("source_file", ""))
            if relative:
                source_files[relative] = str(row.get("source", ""))
        for row in relevant_types:
            relative = str(row.get("file", ""))
            path = output_root / relative
            if relative and relative not in source_files and path.is_file():
                source_files[relative] = path.read_text(encoding="utf-8")
        for relative in (
            "tests/support/runtime.ts", "tests/support/e2e.ts",
            "backend/src/app.ts", "backend/src/db/client.ts",
        ):
            specifier = _relative_import(output_files[layer], relative)
            if not any(row.get("specifier") == specifier for row in allowed_imports[layer]):
                continue
            path = output_root / relative
            if relative not in source_files and path.is_file():
                source_files[relative] = path.read_text(encoding="utf-8")
        layers[layer] = {
            "output_file": output_files[layer],
            "imports": allowed_imports[layer],
        }
        if layer == "E2E":
            layers[layer]["entry_routes"] = _e2e_entry_routes(requirement_id, frontend_subgraph)
            if "root_component_id" in frontend_subgraph:
                layers[layer]["frontend_design"] = frontend_subgraph
                layers[layer]["navigation_guidance"] = (
                    "Start at / and use the designed UI interactions to reach conditional components. "
                    "Components are not automatically URL routes. Test requirement behavior, not placeholder output.")
    return layers, source_files


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
        dependency_ids.update(row["target"] for row in frontend_subgraph.get("effects", [])
                              if row.get("kind") == "REQUEST" and row.get("target"))
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
) -> dict[str, Any]:
    if "root_component_id" in frontend_ir:
        from .frontend_context import frontend_subgraph
        return frontend_subgraph(frontend_ir, requirement_id,
                                 {str(row.get("module_id", "")) for row in owned_targets})
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
    if "root_component_id" in frontend_subgraph:
        return list(frontend_subgraph.get("entry_routes", []))
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
    return sorted({route for route in journey_routes if route} | {
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
        "source_files_chars": size(payload.get("source_files", {})),
        "layers_chars": size(payload.get("layers", {})),
    }
    return (
        f"CONTEXT_AUDIT phase=test_generation requirement={requirement_id} "
        f"attempt={attempt} "
        + " ".join(f"{key}={value}" for key, value in fields.items())
    )


def _plan_test_layers(
    resolved_targets: dict[str, Any],
    design_ir: dict[str, Any],
) -> list[str]:
    owned = [
        row for row in resolved_targets.get("owned_targets", [])
        if isinstance(row, dict)
    ]
    layers: set[str] = set()
    if any(row.get("kind") in {"PAGE", "COMPONENT", "LAYOUT"} for row in owned):
        layers.add("E2E")
    if any(row.get("kind") == "API" for row in owned):
        layers.add("INTEGRATION")

    pure_function_ids = _pure_function_ids(design_ir)
    if any(
        str(row.get("module_id", "")) in pure_function_ids
        for row in owned if row.get("kind") == "FUNC"
    ):
        layers.add("UNIT")
    return [layer for layer in TEST_LAYERS if layer in layers]


def _pure_function_ids(design_ir: dict[str, Any]) -> set[str]:
    modules = {
        str(row.get("id", "")): row
        for row in design_ir.get("modules", [])
        if isinstance(row, dict)
    }
    pure_cache: dict[str, bool] = {}

    def is_pure_func(module_id: str, visiting: set[str] | None = None) -> bool:
        if module_id in pure_cache:
            return pure_cache[module_id]
        module = modules.get(module_id, {})
        if module.get("kind") != "FUNC" or module.get("effects"):
            pure_cache[module_id] = False
            return False
        active = set(visiting or ())
        if module_id in active:
            pure_cache[module_id] = False
            return False
        active.add(module_id)
        pure_cache[module_id] = all(
            is_pure_func(str(callee_id), active)
            for callee_id in module.get("callees", [])
        )
        return pure_cache[module_id]

    return {
        module_id for module_id, row in modules.items()
        if row.get("kind") == "FUNC" and is_pure_func(module_id)
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
    if not re.search(r"\b(?:test|it)(?:\s*\.\s*each)?\s*\(", code):
        errors.append(f"ARC4426 TEST_CODE_INVALID: {layer} contains no executable test declaration.")
    import_rules = [
        row for row in context_pack["allowed_imports"].get(layer, [])
        if isinstance(row, dict)
    ]
    allowed = {
        str(row.get("specifier", "")) for row in import_rules
        if not row.get("type_only")
    }
    type_only = {
        str(row.get("specifier", "")) for row in import_rules
        if row.get("type_only")
    }
    import_source = _without_type_only_imports(code, type_only)
    imports = re.findall(r"\bfrom\s+[\"']([^\"']+)[\"']", import_source)
    imports.extend(
        re.findall(r"\bimport\s+(?:type\s+)?[\"']([^\"']+)[\"']", import_source)
    )
    imports.extend(
        re.findall(
            r"\b(?:import|require)\s*\(\s*[\"']([^\"']+)[\"']\s*\)",
            import_source,
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
        if re.search(r"data-arc-(?:page|component|layout|obligation)\b", code):
            errors.append(
                "ARC4426 TEST_CODE_INVALID: E2E must not rely on temporary "
                "data-arc skeleton attributes; use user-visible locators."
            )
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
    if re.search(r"/__arc/seed|\b(?:seedRequirement|seedFixturesForRequirement)\s*\(", code):
        errors.append(
            f"ARC4428 TEST_SEED_INVALID: {layer} must prepare test data independently of application fixtures."
        )
    if layer == "E2E":
        if re.search(r"\bgetByLabel\s*\(\s*(['\"])[^'\"]+\1\s*\)", code):
            errors.append(
                "ARC4426 TEST_CODE_INVALID: E2E literal getByLabel locators must use "
                "{ exact: true } to avoid ambiguous substring matches."
            )
        allowed_routes = {
            str(row.get("route", "")).strip()
            for row in context_pack.get("relevant_frontend_subgraph", {}).get("screens", [])
            if isinstance(row, dict) and str(row.get("route", "")).strip()
        }
        allowed_routes.update(context_pack.get("e2e_entry_routes", []))
        literal_routes = re.findall(
            r"\bpage\.goto\s*\(\s*[\"']([^\"']+)[\"']\s*\)",
            code,
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
    if layer == "INTEGRATION" and not _uses_integration_base_url(code, imports):
        errors.append(
            "ARC4427 TEST_IMPORT_INVALID: INTEGRATION must import a default Supertest client "
            "and call it with ARC_TEST_BASE_URL directly or via a local URL variable."
        )
    return errors


def _without_type_only_imports(code: str, specifiers: set[str]) -> str:
    for specifier in specifiers:
        quoted = rf"[\"']{re.escape(specifier)}[\"']"
        code = re.sub(
            rf"\bimport\s+type\s+(?:\{{[^}}]*\}}|[A-Za-z_$][\w$]*)\s+from\s*{quoted}\s*;?",
            "",
            code,
        )
        code = re.sub(
            rf"\bimport\s*\{{\s*type\s+[A-Za-z_$][\w$]*"
            rf"(?:\s+as\s+[A-Za-z_$][\w$]*)?"
            rf"(?:\s*,\s*type\s+[A-Za-z_$][\w$]*(?:\s+as\s+[A-Za-z_$][\w$]*)?)*"
            rf"\s*,?\s*\}}\s*from\s*{quoted}\s*;?",
            "",
            code,
        )
        code = re.sub(
            rf"\bimport\s*\(\s*{quoted}\s*\)\s*\.\s*[A-Z][\w]*",
            "",
            code,
        )
    return code


def _uses_integration_base_url(code: str, imports: list[str]) -> bool:
    if "supertest" not in imports:
        return False
    clients = set(re.findall(
        r"\bimport\s+([A-Za-z_$][\w$]*)\s+from\s*[\"']supertest[\"']",
        code,
    ))
    base_urls = set(re.findall(
        r"\b(?:const|let)\s+([A-Za-z_$][\w$]*)\s*(?::\s*string)?\s*=\s*process\.env\.ARC_TEST_BASE_URL\b",
        code,
    ))
    for client in clients:
        if re.search(rf"\b{re.escape(client)}\s*\(\s*process\.env\.ARC_TEST_BASE_URL\b", code):
            return True
        if any(
            re.search(
                rf"\b{re.escape(client)}\s*\(\s*{re.escape(base_url)}\s*(?:!|\s+as\s+string)?\s*\)",
                code,
            )
            for base_url in base_urls
        ):
            return True
    return False


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
    decision: dict[str, Any],
    context_pack: dict[str, Any],
) -> list[dict[str, Any]]:
    files = []
    for row in decision["files"]:
        layer = str(row["layer"]).upper()
        files.append({
            "requirement_id": requirement_id,
            "layer": layer,
            "test_file": str(context_pack["output_files"][layer]),
            "content_sha256": hashlib.sha256((row["code"].rstrip() + "\n").encode("utf-8")).hexdigest(),
        })
    return files
def _layer_source_cards(
    layer: str,
    targets: list[dict[str, Any]],
    test_file: str,
    output_root: Path,
    *,
    pure_function_ids: set[str],
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
        if layer == "UNIT" and str(target.get("module_id", "")) not in pure_function_ids:
            continue
        row = {
            key: copy.deepcopy(target.get(key))
            for key in (
                "module_id",
                "source_ir_id",
                "kind",
                "file",
                "symbol",
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
    testable_ids = {
        str(value)
        for kind in ("atomic_units", "folder_nodes")
        for value in requirement_ir.get(kind, [])
        if str(value)
    }
    if not requirement_id:
        errors.append("ARC4412 TEST_CONTEXT_INVALID: requirement_id is required.")
    elif not isinstance(nodes, dict) or not isinstance(nodes.get(requirement_id), dict):
        errors.append(
            f"ARC4412 TEST_CONTEXT_INVALID: missing requirement {requirement_id}."
        )
    elif requirement_id not in testable_ids:
        errors.append(
            f"ARC4412 TEST_CONTEXT_INVALID: {requirement_id} is not a testable requirement."
        )
    return errors


def _replace_requirement_slice(
    manifest: dict[str, Any], *, requirement_id: str, state: str,
    file_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": state,
        "files": sorted(
            [row for row in manifest.get("files", []) if row.get("requirement_id") != requirement_id]
            + file_rows,
            key=lambda row: row["test_file"],
        ),
    }


def _finalize_manifest(
    manifest: dict[str, Any], *, status: str,
) -> dict[str, Any]:
    return {"status": status, "files": manifest.get("files", [])}
def _test_workspace_path(test_file: str) -> str:
    normalized = str(test_file).replace("\\", "/").strip().strip("/")
    return normalized.removeprefix("tests/")


def _command_output(stdout: str | None, stderr: str | None) -> str:
    value = "\n".join(part for part in (stdout or "", stderr or "") if part)
    return value if value else "no command output"


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
