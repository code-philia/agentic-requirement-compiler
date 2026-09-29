from __future__ import annotations

import os
import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from arc_agents import FrontendImplementationAgent, ImplementationAgent, ImplementationRequest, JsonModel
from arc_agents.contracts import ProposedPatch
from arcbench_agent_runtime.jsonio import write_json_atomic
from core.logging import append_debug_log, write_terminal_log

from .code_binding import CodeTargetResolver
from .exact_file_patcher import ExactFilePatcher
from .frontend_thin_design import project_frontend_runtime_ir
from .git_history import ProjectGitHistory
from .project_build import ProjectBuilder
from .test_generation import TEST_LAYERS, _format_model_log
from .test_runner import TestRunResult, TestRunner, TestSelection


@dataclass(frozen=True, slots=True)
class NodeTDDPolicy:
    max_iterations_per_layer: int = 5

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "NodeTDDPolicy":
        values = os.environ if environment is None else environment
        raw = values.get("ARC_TDD_MAX_ITERATIONS_PER_LAYER", "5")
        try:
            budget = max(1, min(int(raw), 50))
        except (TypeError, ValueError):
            budget = 5
        return cls(max_iterations_per_layer=budget)


@dataclass(slots=True)
class TDDStageResult:
    stage: str
    status: str = "FAILED"
    changed_files: list[str] = field(default_factory=list)
    failed_requirements: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"IMPLEMENTED", "TESTS_PASSED"}


class NodeTDDOrchestrator:
    """Implement and verify a node, allowing diagnosed test corrections."""

    _BACKEND_KINDS = {"DB", "FUNC", "API"}
    _FRONTEND_KINDS = {"API_CLIENT", "STORE", "COMPONENT", "PAGE", "LAYOUT"}

    def __init__(
        self,
        model: JsonModel,
        output_root: Path,
        *,
        requirement_ir: dict[str, Any],
        code_binding_registry: dict[str, Any],
        frontend_ir: dict[str, Any] | None = None,
        test_manifest: dict[str, Any] | None = None,
        policy: NodeTDDPolicy | None = None,
        test_runner: TestRunner | None = None,
        implementation_agent: ImplementationAgent | None = None,
        frontend_implementation_agent: FrontendImplementationAgent | None = None,
        file_patcher: ExactFilePatcher | None = None,
    ) -> None:
        self.output_root = output_root.expanduser().resolve()
        self.requirement_ir = requirement_ir
        self.code_binding_registry = code_binding_registry
        self.frontend_ir = frontend_ir
        runtime_ir = (frontend_ir if frontend_ir is not None and "root_component_id" in frontend_ir else
                      project_frontend_runtime_ir(frontend_ir) if frontend_ir is not None else {})
        self._frontend_pages = {
            str(row["id"]): row for row in runtime_ir.get("pages", []) if isinstance(row, dict)
        }
        self._frontend_components = {
            str(row["id"]): row for row in runtime_ir.get("components", []) if isinstance(row, dict)
        }
        self._binding_by_id = {
            str(row["module_id"]): row
            for row in code_binding_registry.get("code_bindings", []) if isinstance(row, dict)
        }
        self.test_manifest = test_manifest
        self.policy = policy or NodeTDDPolicy.from_environment()
        self.test_runner = test_runner or TestRunner(self.output_root)
        self.implementation_agent = implementation_agent or ImplementationAgent(
            model, self.output_root,
            trace=self._trace_implementation,
            model_log=self._write_implementation_model_log,
        )
        self.frontend_implementation_agent = frontend_implementation_agent or FrontendImplementationAgent(
            model, self.output_root,
            trace=self._trace_frontend_implementation,
            model_log=self._write_implementation_model_log,
        )
        self.file_patcher = file_patcher or ExactFilePatcher(self.output_root)
        self._recent_changes: dict[str, dict[str, dict[str, Any]]] = {}

    def _trace_frontend_implementation(self, message: str) -> None:
        self._trace_implementation(message, frontend=True)

    def _trace_implementation(self, message: str, *, frontend: bool = False) -> None:
        status = "warning" if "MODEL_REJECTED" in message else "info"
        if frontend and "MODEL_REJECTED" in message and "read-only or unknown file" in message:
            attempt = re.search(r"attempt=(\d+)/(\d+)", message)
            retrying = attempt is not None and int(attempt.group(1)) < int(attempt.group(2))
            detail = (
                "The edit was rejected without changing any source. Regenerating with the "
                "writable-file allowlist."
                if retrying else "The edit was rejected without changing any source; retry budget exhausted."
            )
            message = f"ARC4551 FRONTEND_EDIT_SCOPE_WARNING: {message} {detail}"
            status = "warning"
        append_debug_log(
            "NodeTDDOrchestrator", message, status=status,
            workspace_root=str(self.output_root),
        )
        write_terminal_log("NodeTDDOrchestrator", message, status=status)

    def _write_implementation_model_log(self, payload: dict[str, Any]) -> None:
        """Persist complete implementation model exchanges for replay/audit."""
        agent_name = str(payload.get("agent_name", "implementation")).lower()
        phase = "frontend_implementation" if "frontend" in agent_name else "implementation"
        log_root = self.output_root / ".arc" / "model_logs" / phase
        log_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"{time.time_ns() % 1_000_000_000:09d}Z"
        requirement_id = re.sub(
            r"[^A-Za-z0-9_.-]+", "_", str(payload.get("requirement_id", "unknown"))
        )
        attempt = int(payload.get("attempt", 0) or 0)
        event = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(payload.get("event", "MODEL_EXCHANGE")))
        iteration = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(payload.get("iteration", 0)))
        path = log_root / f"{stamp}-{requirement_id}-iteration-{iteration}-attempt-{attempt}-{event}.log"
        text = (
            f"event: {event}\nagent_name: {payload.get('agent_name', '')}\n"
            + _format_model_log(payload)
        )
        path.write_text(text, encoding="utf-8")

    def implement_backend(self, requirement_ids: list[str]) -> TDDStageResult:
        return self._implement_stage(
            "6.1 backend implementation", requirement_ids, self._BACKEND_KINDS,
            self.implementation_agent,
        )

    def implement_frontend(self, requirement_ids: list[str]) -> TDDStageResult:
        return self._implement_stage(
            "6.2 frontend implementation", requirement_ids, self._FRONTEND_KINDS,
            self.frontend_implementation_agent,
        )

    def run_test_layers(
        self,
        requirement_ids: list[str],
        *,
        layers: tuple[str, ...] = TEST_LAYERS,
    ) -> TDDStageResult:
        result = TDDStageResult("6.3-6.5 test-driven repair", status="TESTS_PASSED")
        if not self.test_manifest or self.test_manifest.get("status") != "TESTS_FROZEN":
            return self._fail(result, "Tests must be frozen before TDD.")
        missing = [
            requirement_id for requirement_id in requirement_ids
            if not any(self._has_test(requirement_id, layer) for layer in TEST_LAYERS)
        ]
        if missing:
            result.failed_requirements = missing
            return self._fail(result, "No frozen tests for: " + ", ".join(missing))

        budgets: dict[tuple[str, str], int] = {}
        for layer in layers:
            layer_result = self._run_layer(layer, requirement_ids, budgets)
            result.changed_files = sorted(set(result.changed_files) | set(layer_result.changed_files))
            result.failed_requirements.extend(layer_result.failed_requirements)
            result.errors.extend(layer_result.errors)
            if not layer_result.ok:
                result.stage = layer_result.stage
                result.status = layer_result.status
                result.failed_requirements = sorted(set(result.failed_requirements))
                write_terminal_log(
                    "NodeTDDOrchestrator",
                    f"{layer} failed; continuing to subsequent test layers with their own repair budgets.",
                    status="warning",
                )
        result.failed_requirements = sorted(set(result.failed_requirements))
        return result

    def verify_test_layers(self, requirement_ids: list[str]) -> TDDStageResult:
        """Recheck final code after later layers' repairs, without more repair calls."""
        result = TDDStageResult("final node verification", status="TESTS_PASSED")
        if not self.test_manifest or self.test_manifest.get("status") != "TESTS_FROZEN":
            return self._fail(result, "Tests must be frozen before verification.")
        for requirement_id in requirement_ids:
            layers = [layer for layer in TEST_LAYERS if self._has_test(requirement_id, layer)]
            if not layers:
                result.failed_requirements.append(requirement_id)
                self._fail(result, f"No frozen tests for: {requirement_id}")
            for layer in layers:
                run = self._run_tests(requirement_id, layer)
                if not run.ok or not run.commands:
                    result.failed_requirements.append(requirement_id)
                    self._fail(result, f"{requirement_id} {layer}: {self._raw_output(run)}")
        result.failed_requirements = sorted(set(result.failed_requirements))
        return result

    def _implement_stage(
        self,
        stage: str,
        requirement_ids: list[str],
        allowed_kinds: set[str],
        agent: ImplementationAgent,
    ) -> TDDStageResult:
        result = TDDStageResult(stage, status="IMPLEMENTED")
        if not self.test_manifest or self.test_manifest.get("status") != "TESTS_FROZEN":
            return self._fail(result, "Tests must be frozen before implementation.")
        for requirement_id in requirement_ids:
            if not any(self._has_test(requirement_id, layer) for layer in TEST_LAYERS):
                result.failed_requirements.append(requirement_id)
                return self._fail(result, f"No frozen tests for: {requirement_id}")
            targets = [
                row for row in self._owned_targets(requirement_id)
                if row.get("kind") in allowed_kinds
            ]
            if not targets:
                continue
            requirement = self._requirement(requirement_id)
            phases = (
                [
                    [row for row in targets if row.get("kind") != "PAGE"],
                    [row for row in targets if row.get("kind") == "PAGE"],
                ] if agent is self.frontend_implementation_agent else [targets]
            )
            for phase_targets in phases:
                if not phase_targets:
                    continue
                changed, error = self._apply_edit(
                    requirement_id,
                    requirement,
                    agent,
                    result,
                    target_ids=tuple(str(row["module_id"]) for row in phase_targets),
                    test_files=tuple(
                        str(row["test_file"])
                        for row in (self.test_manifest or {}).get("files", [])
                        if row.get("requirement_id") == requirement_id
                    ),
                )
                if not changed:
                    result.failed_requirements.append(requirement_id)
                    result.errors.append(f"{requirement_id}: {error}")
                    result.status = "IMPLEMENTATION_FAILED"
                    return result
                if agent is self.frontend_implementation_agent:
                    for _ in range(self.policy.max_iterations_per_layer):
                        pending = self._pending_frontend_targets(phase_targets)
                        if not pending:
                            break
                        changed, error = self._apply_edit(
                            requirement_id,
                            requirement,
                            agent,
                            result,
                            target_ids=tuple(str(row["module_id"]) for row in pending),
                            implementation_feedback=(
                                "FRONTEND_INCOMPLETE: finish every placeholder and compose "
                                "shared children instead of duplicating them. CONTENT_SLOT "
                                "form components must wrap the page form and render children. "
                                "Remaining files: "
                                + ", ".join(sorted({str(row["file"]) for row in pending}))
                            ),
                        )
                        if not changed:
                            result.failed_requirements.append(requirement_id)
                            result.errors.append(f"{requirement_id}: {error}")
                            result.status = "IMPLEMENTATION_FAILED"
                            return result
                    pending = self._pending_frontend_targets(phase_targets)
                    if pending:
                        result.failed_requirements.append(requirement_id)
                        result.errors.append(
                            f"{requirement_id}: FRONTEND_INCOMPLETE: unfinished UI or "
                            "uncomposed child in "
                            + ", ".join(sorted({str(row["file"]) for row in pending}))
                        )
                        result.status = "IMPLEMENTATION_FAILED"
                        return result
        return result

    def _pending_frontend_targets(self, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        for row in targets:
            if row.get("kind") not in {"PAGE", "COMPONENT", "LAYOUT"}:
                continue
            source = self.output_root / str(row["file"])
            if not source.is_file():
                pending.append(row)
                continue
            text = source.read_text(encoding="utf-8")
            if any(marker in text for marker in (
                "Implementation pending", "data-arc-obligation=",
                "TODO: Implement", "Not implemented:",
                f"data-arc-{str(row['kind']).lower()}=",
            )):
                pending.append(row)
                continue
            if self.frontend_ir is not None and "root_component_id" in self.frontend_ir \
                    and row.get("kind") == "COMPONENT":
                # The deterministic lowering emits this exact shell until the
                # implementation pass materializes the seven-entity UI tree.
                # Treat an unchanged empty return as incomplete even if a model
                # removed the comment marker.
                if re.search(r"return\s*<React\.Fragment\s*/>\s*;", text):
                    pending.append(row)
                    continue
            if row.get("kind") == "COMPONENT":
                component = self._frontend_components.get(str(row["module_id"]), {})
                if component.get("composition_mode") == "CONTENT_SLOT" and not re.search(
                    r"\{\s*(?:(?:_props|props)\.)?children\s*\}", text,
                ):
                    pending.append(row)
            if row.get("kind") == "PAGE":
                page = self._frontend_pages.get(str(row["module_id"]), {})
                for child_id in page.get("component_ids", []):
                    child = self._frontend_components.get(str(child_id), {})
                    symbol = str(self._binding_by_id.get(str(child_id), {}).get("symbol", ""))
                    if not symbol:
                        continue
                    if not re.search(r"<" + re.escape(symbol) + r"\b", text):
                        pending.append(row)
                        break
                    if child.get("composition_mode") == "CONTENT_SLOT" and not re.search(
                        r"<" + re.escape(symbol) + r"\b[^>]*>"
                        r"(?:(?!</" + re.escape(symbol) + r"\s*>).)*?<form\b"
                        r"(?:(?!</" + re.escape(symbol) + r"\s*>).)*?"
                        r"</" + re.escape(symbol) + r"\s*>", text, re.DOTALL,
                    ):
                        pending.append(row)
                        break
        return pending

    def _run_layer(
        self,
        layer: str,
        requirement_ids: list[str],
        budgets: dict[tuple[str, str], int],
    ) -> TDDStageResult:
        result = TDDStageResult(layer, status="TESTS_PASSED")
        layer_requirements = [
            requirement_id for requirement_id in requirement_ids
            if self._has_test(requirement_id, layer)
        ]
        for requirement_id in layer_requirements:
            run = self._run_tests(requirement_id, layer)
            if not run.commands:
                result.failed_requirements.append(requirement_id)
                return self._fail(result, *run.errors)
            if self._has_infrastructure_error(run):
                result.failed_requirements.append(requirement_id)
                return self._fail(result, self._raw_output(run))
            aggregate_repairs: list[tuple[str, tuple[str, ...], bool]] = []
            scope = CodeTargetResolver(self.code_binding_registry).resolve_requirement_targets(requirement_id)
            has_editable_targets = bool(scope["owned_targets"] or scope["dependency_targets"])
            if layer == "E2E" and not has_editable_targets:
                aggregate_repairs = self._aggregate_repair_targets(requirement_id)
            feedback = self._raw_output(run)
            repair_feedback = ""
            while not run.ok:
                budget_key = (requirement_id, layer)
                if layer == "E2E" and not has_editable_targets and not aggregate_repairs and not run.selected_files:
                    result.failed_requirements.append(requirement_id)
                    return self._fail(
                        result,
                        f"{requirement_id}: no related descendant owns a writable repair target.",
                        feedback,
                    )
                if budgets.get(budget_key, 0) >= self.policy.max_iterations_per_layer:
                    self._trace_implementation(
                        f"REPAIR_BUDGET_EXHAUSTED requirement={requirement_id} layer={layer} "
                        f"iteration={budgets[budget_key]}/{self.policy.max_iterations_per_layer}")
                    result.failed_requirements.append(requirement_id)
                    result.errors.append(feedback)
                    result.status = "BUDGET_EXHAUSTED"
                    return result
                repair_index = budgets.get(budget_key, 0)
                budgets[budget_key] = repair_index + 1
                self._trace_implementation(
                    f"REPAIR_STARTED requirement={requirement_id} layer={layer} "
                    f"iteration={repair_index + 1}/{self.policy.max_iterations_per_layer}")
                agent = (
                    self.frontend_implementation_agent
                    if layer == "E2E" else self.implementation_agent
                )
                repair_id = requirement_id
                target_ids: tuple[str, ...] = ()
                requirement = self._requirement(requirement_id)
                if aggregate_repairs:
                    repair_id, target_ids, is_frontend = aggregate_repairs[repair_index % len(aggregate_repairs)]
                    agent = (
                        self.frontend_implementation_agent
                        if is_frontend else self.implementation_agent
                    )
                    requirement = {
                        **self._requirement(repair_id),
                        "aggregate_requirement": requirement,
                    }
                changed, error = self._apply_edit(
                    repair_id,
                    requirement,
                    agent,
                    result,
                    target_ids=target_ids,
                    test_output=self._raw_output(run),
                    implementation_feedback=repair_feedback,
                    iteration=repair_index + 1,
                    test_layer=layer,
                    test_files=tuple(run.selected_files),
                    commit_stage=f"6.{TEST_LAYERS.index(layer) + 3} {layer.lower()} repair {requirement_id}",
                )
                if not changed:
                    self._trace_implementation(
                        f"REPAIR_REJECTED requirement={requirement_id} layer={layer} "
                        f"iteration={repair_index + 1}/{self.policy.max_iterations_per_layer} error={error}")
                    if "PATCH_ROLLBACK_FAILED:" in error or "DIAGNOSIS_BLOCKED:" in error:
                        result.failed_requirements.append(requirement_id)
                        return self._fail(result, self._raw_output(run), error)
                    repair_feedback = error
                    feedback = self._raw_output(run)
                    continue
                run = self._run_tests(requirement_id, layer)
                repair_feedback = ""
                self._trace_implementation(
                    f"REPAIR_FINISHED requirement={requirement_id} layer={layer} "
                    f"iteration={repair_index + 1}/{self.policy.max_iterations_per_layer} "
                    f"status={'PASSED' if run.ok else 'FAILED'}")
                if not run.commands:
                    result.failed_requirements.append(requirement_id)
                    return self._fail(result, *run.errors)
                if self._has_infrastructure_error(run):
                    result.failed_requirements.append(requirement_id)
                    return self._fail(result, self._raw_output(run))
                feedback = self._raw_output(run)
        return result

    def _aggregate_repair_targets(self, requirement_id: str) -> list[tuple[str, tuple[str, ...], bool]]:
        resolver = CodeTargetResolver(self.code_binding_registry)
        related_ids = {
            str(row["module_id"])
            for row in resolver.resolve_requirement_targets(requirement_id)["dependency_targets"]
        }
        nodes = self.requirement_ir.get("nodes", {})
        candidates: list[tuple[str, tuple[str, ...], bool]] = []
        for atomic_id in self.requirement_ir.get("atomic_units", []):
            current = nodes.get(atomic_id, {}).get("parent_id")
            while current and current != requirement_id:
                current = nodes.get(current, {}).get("parent_id")
            if current != requirement_id:
                continue
            targets = [
                row for row in self._owned_targets(atomic_id)
                if str(row.get("module_id", "")) in related_ids
            ]
            for is_frontend, kinds in ((True, self._FRONTEND_KINDS), (False, self._BACKEND_KINDS)):
                target_ids = tuple(
                    str(row["module_id"]) for row in targets if row.get("kind") in kinds
                )
                if target_ids:
                    candidates.append((atomic_id, target_ids, is_frontend))
        candidates.sort(key=lambda row: (not row[2], row[0]))
        return candidates

    def _apply_edit(
        self,
        requirement_id: str,
        requirement: dict[str, Any],
        agent: ImplementationAgent,
        result: TDDStageResult,
        *,
        target_ids: tuple[str, ...] = (),
        test_output: str = "",
        implementation_feedback: str = "",
        test_files: tuple[str, ...] = (),
        commit_stage: str = "",
        iteration: int = 0,
        test_layer: str = "",
    ) -> tuple[bool, str]:
        snapshot = self.file_patcher.snapshot([
            *self._checkpoint_files(requirement_id), *test_files,
        ])
        changed_files: list[str] = []

        def accept_patch(patch: ProposedPatch) -> list[str]:
            # Diagnosis may authorize an additional dependency file. Capture
            # its original before applying so rejected builds roll it back too.
            extra = sorted({edit.file for edit in patch.edits} - snapshot.keys())
            captured = self.file_patcher.snapshot(extra)
            if set(extra) - captured.keys():
                return ["PATCH_SNAPSHOT_FAILED: cannot capture newly authorized files."]
            snapshot.update(captured)
            applied = self.file_patcher.apply(patch)
            errors = list(applied.errors)
            if applied.ok:
                build = ProjectBuilder(self.output_root).build()
                errors = list(build.errors) if not build.ok else []
                if not build.ok and not errors:
                    errors = ["PROJECT_BUILD_FAILED: build did not pass."]
                if not errors:
                    typecheck = self.test_runner.run_workspace_typecheck()
                    if typecheck.status != "PASSED":
                        errors = [part for part in (
                            "PROJECT_TYPECHECK_FAILED: typecheck did not pass.",
                            typecheck.stdout, typecheck.stderr, typecheck.error,
                        ) if part]
                if not errors:
                    changed_files[:] = applied.changed_files
                    return []
            elif not errors:
                errors = ["PATCH_APPLY_FAILED: patch was not applied."]
            _, restore_errors = self.file_patcher.restore(snapshot)
            return [
                *errors,
                *(f"PATCH_ROLLBACK_FAILED: {error}" for error in restore_errors),
                *([] if restore_errors else [
                    "The rejected patch was rolled back. Return a complete corrected patch "
                    "against the original editable_files, including all required implementation changes.",
                ]),
            ]

        implementation = agent.implement(ImplementationRequest(
            requirement_id=requirement_id,
            requirement=requirement,
            code_binding_registry=self.code_binding_registry,
            target_module_ids=target_ids,
            test_output=test_output,
            test_files=test_files,
            frontend_ir=self.frontend_ir if agent is self.frontend_implementation_agent else None,
            iteration=iteration,
            iteration_limit=self.policy.max_iterations_per_layer,
            test_layer=test_layer,
            implementation_feedback=implementation_feedback,
            recent_changes=tuple(self._recent_changes.get(requirement_id, {}).values()),
        ), accept_patch=accept_patch)
        if not implementation.ok or implementation.patch is None:
            if implementation.status == "DIAGNOSIS_BLOCKED":
                return False, "DIAGNOSIS_BLOCKED: " + "\n".join(implementation.errors)
            return False, "\n".join(implementation.errors)
        corrected_tests = sorted(set(changed_files) & set(test_files))
        if corrected_tests:
            # Keep integrity checking enabled: only re-freeze tests explicitly
            # admitted by the agent's diagnosis and successfully built above.
            try:
                manifest_path = self.output_root / ".arc" / "tests" / "test_manifest.json"
                manifest = copy.deepcopy(self.test_manifest) if self.test_manifest is not None else json.loads(
                    manifest_path.read_text(encoding="utf-8"))
                rows = {row["test_file"]: row for row in manifest.get("files", [])}
                for relative in corrected_tests:
                    rows[relative]["content_sha256"] = hashlib.sha256(
                        (self.output_root / relative).read_bytes()).hexdigest()
                write_json_atomic(manifest_path, manifest)
                if self.test_manifest is not None:
                    self.test_manifest.clear()
                    self.test_manifest.update(manifest)
                else:
                    self.test_manifest = manifest
            except (OSError, ValueError, KeyError) as exc:
                _, restore_errors = self.file_patcher.restore(snapshot)
                return False, "\n".join([
                    f"TEST_CORRECTION_MANIFEST_FAILED: {exc}",
                    *(f"PATCH_ROLLBACK_FAILED: {error}" for error in restore_errors),
                ])
            self._trace_implementation(
                f"TEST_CORRECTION_APPLIED requirement={requirement_id} layer={test_layer} "
                f"iteration={iteration}/{self.policy.max_iterations_per_layer} files={corrected_tests}")
        if commit_stage:
            ProjectGitHistory(self.output_root).commit(commit_stage, [
                *changed_files,
                *([".arc/tests/test_manifest.json"] if corrected_tests else []),
            ])
        result.changed_files = sorted(set(result.changed_files) | set(changed_files))
        history = self._recent_changes.setdefault(requirement_id, {})
        for relative in changed_files:
            history[relative] = {
                "requirement_id": requirement_id, "iteration": iteration,
                "changed_files": [relative],
                "edits": [{"file": edit.file, "search": edit.search, "replacement": edit.replacement}
                          for edit in implementation.patch.edits if edit.file == relative],
            }
        return True, ""

    def _owned_targets(self, requirement_id: str) -> list[dict[str, Any]]:
        resolved = CodeTargetResolver(self.code_binding_registry).resolve_requirement_targets(requirement_id)
        priority = {
            "DB": 0, "FUNC": 1, "API": 2, "API_CLIENT": 3, "STORE": 4,
            "COMPONENT": 5, "PAGE": 6, "LAYOUT": 7,
        }
        return sorted(
            resolved["owned_targets"],
            key=lambda row: (priority.get(str(row.get("kind", "")), 99), str(row.get("module_id", ""))),
        )

    def _checkpoint_files(self, requirement_id: str) -> list[str]:
        resolved = CodeTargetResolver(self.code_binding_registry).resolve_requirement_targets(requirement_id)
        return sorted({
            str(row["file"])
            for row in [*resolved["owned_targets"], *resolved["dependency_targets"]]
            if row.get("file")
        })

    def _requirement(self, requirement_id: str) -> dict[str, Any]:
        nodes = self.requirement_ir.get("nodes", {})
        if not isinstance(nodes, dict) or not isinstance(nodes.get(requirement_id), dict):
            raise KeyError(f"Unknown requirement: {requirement_id}")
        return nodes[requirement_id]

    def _has_test(self, requirement_id: str, layer: str) -> bool:
        return any(
            isinstance(row, dict)
            and row.get("requirement_id") == requirement_id
            and row.get("layer") == layer
            for row in (self.test_manifest or {}).get("files", [])
        )

    def _run_tests(self, requirement_id: str, layer: str) -> TestRunResult:
        return self.test_runner.run(
            TestSelection(requirement_id=requirement_id, layers=(layer,)),
            test_manifest=self.test_manifest,
        )

    @staticmethod
    def _has_infrastructure_error(run: TestRunResult) -> bool:
        return any(command.status == "ERROR" for command in run.commands)

    @staticmethod
    def _raw_output(run: TestRunResult) -> str:
        parts: list[str] = []
        for command in run.commands:
            parts.extend((command.stdout, command.stderr, command.error or ""))
        if not run.commands:
            parts.extend(run.errors)
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _fail(result: TDDStageResult, *errors: str) -> TDDStageResult:
        result.status = "FAILED"
        result.errors.extend(error for error in errors if error)
        return result
