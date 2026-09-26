from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from arc_agents import FrontendImplementationAgent, ImplementationAgent, ImplementationRequest, JsonModel
from core.logging import SynchronousLog

from .artifacts import CompilerArtifactStore
from .code_binding import CodeTargetResolver
from .exact_file_patcher import ExactFilePatcher
from .project_build import ProjectBuilder
from .test_generation import RequirementTestGenerationPass, TEST_LAYERS
from .test_runner import TestRunResult, TestRunner, TestSelection


@dataclass(frozen=True, slots=True)
class NodeTDDPolicy:
    max_iterations_per_node: int = 10
    initial_target_retry_count: int = 2

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "NodeTDDPolicy":
        values = os.environ if environment is None else environment
        def budget(name: str, default: int) -> int:
            try:
                return max(1, min(int(values.get(name, default)), 50))
            except (TypeError, ValueError):
                return default
        return cls(
            max_iterations_per_node=budget("ARC_TDD_MAX_ITERATIONS_PER_NODE", 10),
            initial_target_retry_count=budget("ARC_TDD_INITIAL_TARGET_RETRY_COUNT", 2),
        )


@dataclass(slots=True)
class NodeTDDResult:
    requirement_id: str
    status: str = "INTERNAL_ERROR"
    iterations: int = 0
    changed_files: list[str] = field(default_factory=list)
    layer_outcomes: dict[str, str] = field(default_factory=dict)
    checkpoint_files: list[str] = field(default_factory=list)
    compile_accepted_targets: list[str] = field(default_factory=list)
    incomplete_targets: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"NODE_ACCEPTED", "AGGREGATE_ACCEPTED", "NO_IMPLEMENTATION_REQUIRED"}


class NodeTDDOrchestrator:
    def __init__(
        self,
        model: JsonModel,
        output_root: Path,
        *,
        requirement_ir: dict[str, Any],
        dependency_graph: dict[str, Any],
        database_schema: dict[str, Any],
        design_ir: dict[str, Any],
        frontend_ir: dict[str, Any],
        code_binding_registry: dict[str, Any],
        environment_manifest: dict[str, Any],
        artifact_store: CompilerArtifactStore | None = None,
        test_manifest: dict[str, Any] | None = None,
        policy: NodeTDDPolicy | None = None,
        test_generation: RequirementTestGenerationPass | None = None,
        test_runner: TestRunner | None = None,
        implementation_agent: ImplementationAgent | None = None,
        frontend_implementation_agent: FrontendImplementationAgent | None = None,
        file_patcher: ExactFilePatcher | None = None,
    ) -> None:
        self.output_root = output_root.expanduser().resolve()
        self.requirement_ir = requirement_ir
        self.dependency_graph = dependency_graph
        self.database_schema = database_schema
        self.design_ir = design_ir
        self.frontend_ir = frontend_ir
        self.code_binding_registry = code_binding_registry
        self.environment_manifest = environment_manifest
        self.test_manifest = test_manifest
        self.policy = policy or NodeTDDPolicy.from_environment()
        self.artifact_store = artifact_store or CompilerArtifactStore(self.output_root)
        self.test_generation = test_generation or RequirementTestGenerationPass(
            model, self.output_root, self.artifact_store
        )
        self.test_runner = test_runner or TestRunner(self.output_root)
        self.implementation_agent = implementation_agent or ImplementationAgent(model, self.output_root)
        self.frontend_implementation_agent = frontend_implementation_agent or FrontendImplementationAgent(
            model, self.output_root
        )
        self.file_patcher = file_patcher or ExactFilePatcher(self.output_root)
        self.node_states: dict[str, str] = {}
        self._accepted_results: dict[str, NodeTDDResult] = {}
        self._log = SynchronousLog("NodeTDDOrchestrator", workspace_root=self.output_root)

    def run_node(self, requirement_id: str) -> NodeTDDResult:
        result = NodeTDDResult(requirement_id)
        dependencies = self.dependency_graph.get("atomic_dependencies", {}).get(requirement_id, [])
        if any(self.node_states.get(str(dep)) != "NODE_ACCEPTED" for dep in dependencies):
            return self._finish(result, "BLOCKED_DEPENDENCY", ["Dependent requirement has not passed."])
        try:
            generation = self.test_generation.generate_requirement(
                requirement_id=requirement_id,
                requirement_ir=self.requirement_ir,
                database_schema=self.database_schema,
                design_ir=self.design_ir,
                frontend_ir=self.frontend_ir,
                code_binding_registry=self.code_binding_registry,
                environment_manifest=self.environment_manifest,
                existing_manifest=self.test_manifest,
            )
            result.artifacts.update(generation.artifacts)
            if not generation.ok:
                return self._finish(result, "BLOCKED_TEST_MATERIALIZATION", generation.errors)
            self.test_manifest = generation.manifest
            layers = [
                layer for layer in TEST_LAYERS
                if any(row.get("requirement_id") == requirement_id and row.get("layer") == layer
                       for row in self.test_manifest.get("files", []))
            ]
            if not layers:
                return self._finish(result, "BLOCKED_TEST_MATERIALIZATION", ["No tests generated."])
            targets = self._owned_targets(requirement_id)
            initial_errors = self._implement_targets(requirement_id, targets, result)
            if initial_errors:
                return self._finish(result, "NODE_COMPLETED_WITH_FAILURES", initial_errors)
            attempts_by_layer = {layer: 0 for layer in layers}
            position = 0
            while position < len(layers):
                layer = layers[position]
                run = self._run_tests(requirement_id, layer)
                if not run.commands:
                    return self._finish(result, "BLOCKED_TEST_MATERIALIZATION", run.errors)
                if any(command.status == "ERROR" for command in run.commands):
                    return self._finish(result, "BLOCKED_INFRA", run.errors or [self._raw_output(run)])
                patch_error = ""
                while not run.ok and attempts_by_layer[layer] < self.policy.max_iterations_per_node:
                    attempts_by_layer[layer] += 1
                    result.iterations += 1
                    output = patch_error or self._raw_output(run)
                    changed, error = self._apply_edit(
                        requirement_id, self.requirement_ir["nodes"][requirement_id],
                        self.implementation_agent, result, test_output=output,
                        test_files=tuple(run.selected_files),
                    )
                    if not changed and error:
                        patch_error = error
                        self._log.info(f"TDD_PATCH_FAILED requirement={requirement_id} layer={layer}: {error}")
                    if changed and position:
                        patch_error = ""
                        for earlier_position, earlier in enumerate(layers[:position]):
                            regression = self._run_tests(requirement_id, earlier)
                            if not regression.ok:
                                position = earlier_position
                                layer = earlier
                                run = regression
                                break
                        else:
                            run = self._run_tests(requirement_id, layer)
                    elif changed:
                        patch_error = ""
                        run = self._run_tests(requirement_id, layer)
                    if not run.commands:
                        return self._finish(result, "BLOCKED_TEST_MATERIALIZATION", run.errors)
                    if any(command.status == "ERROR" for command in run.commands):
                        return self._finish(result, "BLOCKED_INFRA", run.errors or [self._raw_output(run)])
                if not run.ok:
                    result.layer_outcomes[layer] = "BUDGET_EXHAUSTED"
                    for skipped in layers[position + 1:]:
                        result.layer_outcomes[skipped] = "SKIPPED"
                    return self._finish(result, "NODE_COMPLETED_WITH_FAILURES", [self._raw_output(run)])
                result.layer_outcomes[layer] = "PASSED"
                position += 1
            for accepted_id in self._impacted_requirements(requirement_id, result.changed_files):
                regression = self.test_runner.run(
                    TestSelection(requirement_id=accepted_id, include_typecheck=False),
                    test_manifest=self.test_manifest,
                    environment_manifest=self.environment_manifest,
                )
                if not regression.ok:
                    return self._finish(result, "REGRESSION_FAILED", [self._raw_output(regression)])
            return self._finish(result, "NODE_ACCEPTED", [])
        except Exception as exc:
            return self._finish(result, "INTERNAL_ERROR", [f"{type(exc).__name__}: {exc}"])

    def run_aggregate_node(self, requirement_id: str) -> NodeTDDResult:
        result = NodeTDDResult(requirement_id)
        try:
            targets = [
                row for row in self._owned_targets(requirement_id)
                if row.get("kind") in {"STORE", "COMPONENT", "PAGE", "LAYOUT"}
            ]
            if not targets:
                return self._finish(result, "NO_IMPLEMENTATION_REQUIRED", [])
            errors = self._implement_targets(requirement_id, targets, result, aggregate=True)
            result.compile_accepted_targets = [
                row["module_id"] for row in targets if row["module_id"] not in result.incomplete_targets
            ]
            for accepted_id in sorted(self._accepted_results):
                if self._accepted_results[accepted_id].status != "NODE_ACCEPTED":
                    continue
                regression = self.test_runner.run(
                    TestSelection(requirement_id=accepted_id, include_typecheck=False),
                    test_manifest=self.test_manifest,
                    environment_manifest=self.environment_manifest,
                )
                if not regression.ok:
                    errors.append(self._raw_output(regression))
                    break
            return self._finish(
                result,
                "AGGREGATE_IMPLEMENTATION_INCOMPLETE" if errors else "AGGREGATE_ACCEPTED",
                errors,
            )
        except Exception as exc:
            return self._finish(result, "INTERNAL_ERROR", [f"{type(exc).__name__}: {exc}"])

    def _owned_targets(self, requirement_id: str) -> list[dict[str, Any]]:
        resolved = CodeTargetResolver(self.code_binding_registry).resolve_requirement_targets(requirement_id)
        priority = {"DB": 0, "FUNC": 1, "API": 2, "STORE": 3, "COMPONENT": 4, "PAGE": 5, "LAYOUT": 6}
        return sorted(resolved["owned_targets"], key=lambda row: (
            priority.get(row.get("kind", ""), 99), str(row.get("module_id", ""))
        ))

    def _implement_targets(
        self, requirement_id: str, targets: list[dict[str, Any]],
        result: NodeTDDResult, *, aggregate: bool = False,
    ) -> list[str]:
        errors: list[str] = []
        requirement = self.requirement_ir["nodes"][requirement_id]
        test_files = tuple(
            str(row["test_file"]) for row in (self.test_manifest or {}).get("files", [])
            if row.get("requirement_id") == requirement_id
        ) if not aggregate else ()
        for target in targets:
            if target.get("kind") not in {"DB", "FUNC", "API", "STORE", "COMPONENT", "PAGE", "LAYOUT"}:
                continue
            agent = self._agent_for_kind(str(target["kind"]))
            completed = False
            feedback = ""
            for attempt in range(self.policy.initial_target_retry_count + 1):
                changed, feedback = self._apply_edit(
                    requirement_id, requirement, agent, result,
                    target_ids=(str(target["module_id"]),), test_output=feedback,
                    test_files=test_files,
                )
                result.iterations += 1
                if changed:
                    completed = True
                    break
            if not completed:
                result.incomplete_targets.append(str(target["module_id"]))
                errors.append(str(target["module_id"]) + ": " + feedback)
        return errors

    def _apply_edit(
        self, requirement_id: str, requirement: dict[str, Any], agent: ImplementationAgent,
        result: NodeTDDResult, *, target_ids: tuple[str, ...] = (),
        test_output: str = "", test_files: tuple[str, ...] = (),
    ) -> tuple[bool, str]:
        snapshot = self.file_patcher.snapshot(self._checkpoint_files(requirement_id))
        implementation = agent.implement(ImplementationRequest(
            requirement_id=requirement_id, requirement=requirement,
            code_binding_registry=self.code_binding_registry,
            target_module_ids=target_ids, test_output=test_output,
            test_files=test_files,
        ))
        if not implementation.ok or implementation.patch is None:
            return False, "\n".join(implementation.errors)
        applied = self.file_patcher.apply(implementation.patch)
        if not applied.ok:
            return False, "\n".join(applied.errors)
        build = ProjectBuilder(self.output_root).build()
        typecheck = self.test_runner.run_workspace_typecheck()
        if not build.ok or typecheck.status != "PASSED":
            self.file_patcher.restore(snapshot)
            detail = "\n".join([*build.errors, typecheck.stdout, typecheck.stderr, typecheck.error or ""])
            return False, detail
        result.changed_files = sorted(set(result.changed_files) | set(applied.changed_files))
        return True, ""

    def _run_tests(self, requirement_id: str, layer: str) -> TestRunResult:
        return self.test_runner.run(
            TestSelection(requirement_id=requirement_id, layers=(layer,), include_typecheck=True),
            test_manifest=self.test_manifest,
            environment_manifest=self.environment_manifest,
        )

    @staticmethod
    def _raw_output(run: TestRunResult) -> str:
        parts: list[str] = []
        for command in run.commands:
            parts.extend((command.stdout, command.stderr))
            for capture_path in (command.capture_progress_path, command.capture_stub_path):
                if capture_path:
                    path = Path(capture_path)
                    if path.is_file():
                        parts.append(path.read_text(encoding="utf-8", errors="replace"))
            if command.error:
                parts.append(command.error)
        if not run.commands:
            parts.extend(run.errors)
        return "\n".join(part for part in parts if part)

    def _agent_for_kind(self, kind: str) -> ImplementationAgent:
        return self.frontend_implementation_agent if kind in {"STORE", "COMPONENT", "PAGE", "LAYOUT"} else self.implementation_agent

    def _checkpoint_files(self, requirement_id: str) -> list[str]:
        return sorted({
            str(row["file"]) for row in self._owned_targets(requirement_id) if row.get("file")
        })

    def _impacted_requirements(self, requirement_id: str, changed_files: list[str]) -> list[str]:
        if not changed_files:
            return []
        bindings = self.code_binding_registry.get("code_bindings", [])
        changed_modules = {
            str(row["module_id"]) for row in bindings if row.get("file") in changed_files
        }
        return sorted(
            other for other, result in self._accepted_results.items()
            if other != requirement_id and result.status == "NODE_ACCEPTED"
            and any(row.get("requirement_id") == other and changed_modules.intersection(row.get("dependencies", []))
                    for row in self.code_binding_registry.get("requirement_targets", []))
        )

    def _finish(self, result: NodeTDDResult, status: str, errors: list[str]) -> NodeTDDResult:
        result.status = status
        result.errors = [error for error in errors if error]
        result.checkpoint_files = self._checkpoint_files(result.requirement_id)
        self.node_states[result.requirement_id] = status
        if result.ok:
            self._accepted_results[result.requirement_id] = copy.deepcopy(result)
        return result
