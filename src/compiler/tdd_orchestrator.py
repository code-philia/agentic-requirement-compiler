from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from arc_agents import FrontendImplementationAgent, ImplementationAgent, ImplementationRequest, JsonModel

from .code_binding import CodeTargetResolver
from .exact_file_patcher import ExactFilePatcher
from .git_history import ProjectGitHistory
from .project_build import ProjectBuilder
from .test_generation import TEST_LAYERS
from .test_runner import TestRunResult, TestRunner, TestSelection


@dataclass(frozen=True, slots=True)
class NodeTDDPolicy:
    max_iterations_per_layer: int = 10

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "NodeTDDPolicy":
        values = os.environ if environment is None else environment
        raw = values.get("ARC_TDD_MAX_ITERATIONS_PER_LAYER", "10")
        try:
            budget = max(1, min(int(raw), 50))
        except (TypeError, ValueError):
            budget = 10
        return cls(max_iterations_per_layer=budget)


@dataclass(slots=True)
class TDDStageResult:
    stage: str
    status: str = "FAILED"
    changed_files: list[str] = field(default_factory=list)
    failed_requirements: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    retry_layer: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"IMPLEMENTED", "TESTS_PASSED"}


class NodeTDDOrchestrator:
    """Run the fixed TDD stages after all tests have been frozen."""

    _BACKEND_KINDS = {"DB", "FUNC", "API"}
    _FRONTEND_KINDS = {"API_CLIENT", "STORE", "COMPONENT", "PAGE", "LAYOUT"}

    def __init__(
        self,
        model: JsonModel,
        output_root: Path,
        *,
        requirement_ir: dict[str, Any],
        code_binding_registry: dict[str, Any],
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
        self.test_manifest = test_manifest
        self.policy = policy or NodeTDDPolicy.from_environment()
        self.test_runner = test_runner or TestRunner(self.output_root)
        self.implementation_agent = implementation_agent or ImplementationAgent(model, self.output_root)
        self.frontend_implementation_agent = frontend_implementation_agent or FrontendImplementationAgent(
            model, self.output_root
        )
        self.file_patcher = file_patcher or ExactFilePatcher(self.output_root)

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

    def run_test_layers(self, requirement_ids: list[str]) -> TDDStageResult:
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
        position = 0
        while position < len(TEST_LAYERS):
            layer = TEST_LAYERS[position]
            layer_result = self._run_layer(
                layer, requirement_ids, budgets, TEST_LAYERS[:position],
            )
            result.changed_files = sorted(set(result.changed_files) | set(layer_result.changed_files))
            result.failed_requirements.extend(layer_result.failed_requirements)
            result.errors.extend(layer_result.errors)
            if layer_result.retry_layer is not None:
                position = TEST_LAYERS.index(layer_result.retry_layer)
                continue
            if not layer_result.ok:
                result.stage = layer_result.stage
                result.status = layer_result.status
                result.failed_requirements = sorted(set(result.failed_requirements))
                return result
            position += 1
        result.failed_requirements = sorted(set(result.failed_requirements))
        result.errors.clear()
        return result

    def _implement_stage(
        self,
        stage: str,
        requirement_ids: list[str],
        allowed_kinds: set[str],
        agent: ImplementationAgent,
    ) -> TDDStageResult:
        result = TDDStageResult(stage, status="IMPLEMENTED")
        for requirement_id in requirement_ids:
            targets = [
                row for row in self._owned_targets(requirement_id)
                if row.get("kind") in allowed_kinds
            ]
            if not targets:
                continue
            requirement = self._requirement(requirement_id)
            changed, error = self._apply_edit(
                requirement_id,
                requirement,
                agent,
                result,
                target_ids=tuple(str(target["module_id"]) for target in targets),
            )
            if not changed:
                result.failed_requirements.append(requirement_id)
                result.errors.append(f"{requirement_id}: {error}")
                result.status = "IMPLEMENTATION_FAILED"
                return result
        return result

    def _run_layer(
        self,
        layer: str,
        requirement_ids: list[str],
        budgets: dict[tuple[str, str], int],
        previous_layers: tuple[str, ...],
    ) -> TDDStageResult:
        result = TDDStageResult(layer, status="TESTS_PASSED")
        layer_requirements = [
            requirement_id for requirement_id in requirement_ids
            if self._has_test(requirement_id, layer)
        ]
        for index, requirement_id in enumerate(layer_requirements):
            run = self._run_tests(requirement_id, layer)
            if not run.commands:
                result.failed_requirements.append(requirement_id)
                return self._fail(result, *run.errors)
            if self._has_infrastructure_error(run):
                result.failed_requirements.append(requirement_id)
                return self._fail(result, self._raw_output(run))
            feedback = self._raw_output(run)
            while not run.ok:
                budget_key = (requirement_id, layer)
                if budgets.get(budget_key, 0) >= self.policy.max_iterations_per_layer:
                    result.failed_requirements.append(requirement_id)
                    result.errors.append(feedback)
                    result.status = "BUDGET_EXHAUSTED"
                    return result
                budgets[budget_key] = budgets.get(budget_key, 0) + 1
                agent = (
                    self.frontend_implementation_agent
                    if layer == "E2E" else self.implementation_agent
                )
                changed, error = self._apply_edit(
                    requirement_id,
                    self._requirement(requirement_id),
                    agent,
                    result,
                    test_output=feedback,
                    test_files=tuple(run.selected_files),
                    commit_stage=f"6.{TEST_LAYERS.index(layer) + 3} {layer.lower()} repair {requirement_id}",
                )
                if not changed:
                    if "PATCH_ROLLBACK_FAILED:" in error:
                        result.failed_requirements.append(requirement_id)
                        return self._fail(result, self._raw_output(run), error)
                    feedback = "\n".join(part for part in (self._raw_output(run), error) if part)
                    continue
                run = self._run_tests(requirement_id, layer)
                if not run.commands:
                    result.failed_requirements.append(requirement_id)
                    return self._fail(result, *run.errors)
                if self._has_infrastructure_error(run):
                    result.failed_requirements.append(requirement_id)
                    return self._fail(result, self._raw_output(run))
                feedback = self._raw_output(run)
                for earlier_requirement in layer_requirements[:index]:
                    regression = self._run_tests(earlier_requirement, layer)
                    if not regression.commands or self._has_infrastructure_error(regression):
                        result.failed_requirements.append(earlier_requirement)
                        return self._fail(result, self._raw_output(regression))
                    if not regression.ok:
                        result.retry_layer = layer
                        result.status = "REGRESSION"
                        result.errors.append(self._raw_output(regression))
                        return result
                for previous in reversed(previous_layers):
                    for previous_requirement in requirement_ids:
                        if not self._has_test(previous_requirement, previous):
                            continue
                        regression = self._run_tests(previous_requirement, previous)
                        if not regression.commands or self._has_infrastructure_error(regression):
                            result.failed_requirements.append(previous_requirement)
                            return self._fail(result, self._raw_output(regression))
                        if not regression.ok:
                            result.retry_layer = previous
                            result.status = "REGRESSION"
                            result.errors.append(self._raw_output(regression))
                            return result
        return result

    def _apply_edit(
        self,
        requirement_id: str,
        requirement: dict[str, Any],
        agent: ImplementationAgent,
        result: TDDStageResult,
        *,
        target_ids: tuple[str, ...] = (),
        test_output: str = "",
        test_files: tuple[str, ...] = (),
        commit_stage: str = "",
    ) -> tuple[bool, str]:
        snapshot = self.file_patcher.snapshot(self._checkpoint_files(requirement_id))
        implementation = agent.implement(ImplementationRequest(
            requirement_id=requirement_id,
            requirement=requirement,
            code_binding_registry=self.code_binding_registry,
            target_module_ids=target_ids,
            test_output=test_output,
            test_files=test_files,
        ))
        if not implementation.ok or implementation.patch is None:
            return False, "\n".join(implementation.errors)
        applied = self.file_patcher.apply(implementation.patch)
        if not applied.ok:
            return False, "\n".join(applied.errors)
        build = ProjectBuilder(self.output_root).build()
        if not build.ok:
            _, restore_errors = self.file_patcher.restore(snapshot)
            return False, "\n".join([
                *build.errors,
                *(f"PATCH_ROLLBACK_FAILED: {error}" for error in restore_errors),
            ])
        typecheck = self.test_runner.run_workspace_typecheck()
        if typecheck.status != "PASSED":
            _, restore_errors = self.file_patcher.restore(snapshot)
            return False, "\n".join([
                typecheck.stdout, typecheck.stderr, typecheck.error or "",
                *(f"PATCH_ROLLBACK_FAILED: {error}" for error in restore_errors),
            ])
        if commit_stage:
            ProjectGitHistory(self.output_root).commit(commit_stage, applied.changed_files)
        result.changed_files = sorted(set(result.changed_files) | set(applied.changed_files))
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
        return sorted({
            str(row["file"])
            for row in self._owned_targets(requirement_id)
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
