from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from arc_agents import (
    FrontendImplementationAgent,
    ImplementationAgent,
    ImplementationRequest,
    JsonModel,
)
from arcbench_agent_runtime.jsonio import write_json_atomic
from core.logging import SynchronousLog

from .artifacts import CompilerArtifactStore
from .code_binding import CodeTargetResolver
from .exact_file_patcher import ExactFilePatcher
from .failure_analysis import FailureAnalysisResult, FailureAnalyzer, TestFailureReport
from .e2e_repair_router import classify_e2e_route
from .test_generation import RequirementTestGenerationPass, TEST_LAYERS
from .test_runner import TestRunResult, TestRunner, TestSelection


NODE_TDD_SCHEMA_VERSION = 1
ACTIONABLE_FAILURE_CLASSES = (
    "TYPE_CONTRACT",
    "IMPLEMENTATION_BEHAVIOR",
    "VISUAL_BEHAVIOR",
    "TEST_RUN_TIMEOUT",
)
TERMINAL_NODE_STATES = {
    "NODE_ACCEPTED",
    "BLOCKED_DEPENDENCY",
    "BLOCKED_INFRA",
    "BLOCKED_TEST_MATERIALIZATION",
    "BLOCKED_CONTRACT",
    "RED_NOT_OBSERVED",
    "AGENT_FAILED",
    "AGGREGATE_IMPLEMENTATION_INCOMPLETE",
    "AGGREGATE_ACCEPTED",
    "NO_IMPLEMENTATION_REQUIRED",
    "PATCH_REJECTED",
    "NO_PROGRESS",
    "ITERATION_BUDGET_EXHAUSTED",
    "NODE_COMPLETED_WITH_FAILURES",
    "REGRESSION_FAILED",
    "INTERNAL_ERROR",
}


@dataclass(frozen=True, slots=True)
class NodeTDDPolicy:
    """Budgets for one requirement-local TDD run."""

    max_iterations_per_node: int = 10
    no_progress_limit: int = 3
    infra_retry_count: int = 2
    initial_target_retry_count: int = 2

    def __post_init__(self) -> None:
        for name in (
            "max_iterations_per_node",
            "no_progress_limit",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1.")
        if self.infra_retry_count < 0:
            raise ValueError("infra_retry_count must not be negative.")
        if self.initial_target_retry_count < 0:
            raise ValueError("initial_target_retry_count must not be negative.")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "NodeTDDPolicy":
        values = os.environ if environment is None else environment
        return cls(
            max_iterations_per_node=_bounded_int(
                values, "ARC_TDD_MAX_ITERATIONS_PER_NODE", 10, 1, 50
            ),
            no_progress_limit=_bounded_int(
                values, "ARC_TDD_NO_PROGRESS_LIMIT", 3, 1, 10
            ),
            infra_retry_count=_bounded_int(
                values, "ARC_TDD_INFRA_RETRY_COUNT", 2, 0, 10
            ),
            initial_target_retry_count=_bounded_int(
                values, "ARC_TDD_INITIAL_TARGET_RETRY_COUNT", 2, 0, 10
            ),
        )


@dataclass(slots=True)
class NodeTDDResult:
    requirement_id: str
    status: str
    iterations: int = 0
    visual_iterations: int = 0
    infrastructure_retries: int = 0
    state_history: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    impacted_requirements: list[str] = field(default_factory=list)
    layer_outcomes: dict[str, str] = field(default_factory=dict)
    checkpoint_files: list[str] = field(default_factory=list)
    incomplete_targets: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    schema_version: int = NODE_TDD_SCHEMA_VERSION

    @property
    def ok(self) -> bool:
        return self.status in {
            "NODE_ACCEPTED",
            "AGGREGATE_ACCEPTED",
            "NO_IMPLEMENTATION_REQUIRED",
        } and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _RunAnalysis:
    test_run: TestRunResult
    analysis: FailureAnalysisResult
    infrastructure_retries: int


@dataclass(slots=True)
class _CompilableCheckpoint:
    sources: dict[str, str]
    changed_files: list[str] = field(default_factory=list)


class NodeTDDOrchestrator:
    """Own the deterministic RED-to-GREEN lifecycle for exactly one node.

    The implementation model can only propose exact file edits. This class
    owns test execution, failure routing, exact patch persistence, budgets, state,
    regression checks, and artifacts. Failed nodes retain their latest
    workspace-typecheck-passing source checkpoint rather than reverting to the
    original skeleton.
    """

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
        failure_analyzer: FailureAnalyzer | None = None,
        implementation_agent: ImplementationAgent | None = None,
        frontend_implementation_agent: FrontendImplementationAgent | None = None,
        file_patcher: ExactFilePatcher | None = None,
        resume: bool = False,
    ) -> None:
        self.output_root = output_root.expanduser().resolve()
        self.requirement_ir = copy.deepcopy(requirement_ir)
        self.dependency_graph = copy.deepcopy(dependency_graph)
        self.database_schema = copy.deepcopy(database_schema)
        self.design_ir = copy.deepcopy(design_ir)
        self.frontend_ir = copy.deepcopy(frontend_ir)
        self.code_binding_registry = copy.deepcopy(code_binding_registry)
        self.environment_manifest = copy.deepcopy(environment_manifest)
        self.test_manifest = copy.deepcopy(test_manifest) if test_manifest else None
        self.policy = policy or NodeTDDPolicy.from_environment()
        self.artifact_store = artifact_store or CompilerArtifactStore(self.output_root)
        self._log = SynchronousLog(
            "NodeTDDOrchestrator", workspace_root=self.output_root
        )

        self.test_generation = test_generation or RequirementTestGenerationPass(
            model,
            self.output_root,
            self.artifact_store,
        )
        self.test_runner = test_runner or TestRunner(self.output_root)
        self.failure_analyzer = failure_analyzer or FailureAnalyzer(
            self.output_root,
            model=model,
        )
        self.implementation_agent = implementation_agent or ImplementationAgent(
            model,
            self.output_root,
            trace=self._trace_implementation,
        )
        self.frontend_implementation_agent = (
            frontend_implementation_agent
            or FrontendImplementationAgent(
                model,
                self.output_root,
                trace=self._trace_implementation,
            )
        )
        self.file_patcher = file_patcher or ExactFilePatcher(self.output_root)

        self.node_states: dict[str, str] = {}
        self._state_history: dict[str, list[str]] = {}
        self._accepted_results: dict[str, NodeTDDResult] = {}
        self._node_checkpoints: dict[str, _CompilableCheckpoint] = {}
        self._layer_gate_outcomes: dict[str, dict[str, str]] = {}
        self._tdd_root = self.output_root / ".arc" / "tdd"
        if resume and self.test_manifest is not None:
            self._restore_accepted_checkpoints()

    def _trace_implementation(self, message: str) -> None:
        self._log.info(message)

    def _implementation_agent_for_reports(
        self,
        reports: list[TestFailureReport],
    ) -> ImplementationAgent:
        type_reports = [
            report
            for report in reports
            if str(getattr(report, "failure_class", "")).upper() == "TYPE_CONTRACT"
            or str(getattr(report, "phase", "")).upper() == "TYPECHECK"
        ]
        if type_reports:
            # TypeScript already tells us which files failed.  Use the binding
            # registry to route a genuinely frontend-only type error to the
            # frontend specialist; retain the general agent for backend,
            # cross-layer, read-only, or otherwise unmapped diagnostics.
            bindings = {
                str(row.get("module_id", "")): row
                for row in self.code_binding_registry.get("code_bindings", [])
                if isinstance(row, dict) and str(row.get("module_id", ""))
            }
            diagnostic_ids = {
                str(module_id)
                for report in type_reports
                for module_id in report.target_modules
                if str(module_id)
            }
            writable_ids = {
                str(target.get("module_id", ""))
                for report in type_reports
                for target in report.writable_targets
                if isinstance(target, dict) and str(target.get("module_id", ""))
            }
            frontend_kinds = {"PAGE", "COMPONENT", "LAYOUT", "STORE"}
            # Some typecheck diagnostics cannot be mapped to a concrete module
            # id, but their writable target set is still precise.  Fall back to
            # that set instead of incorrectly routing a frontend-only error to
            # the general agent merely because target_modules is empty.
            routing_ids = diagnostic_ids or writable_ids
            all_frontend = bool(routing_ids) and all(
                str(bindings.get(module_id, {}).get("kind", "")).upper()
                in frontend_kinds
                for module_id in routing_ids
            )
            fully_writable = not diagnostic_ids or diagnostic_ids <= writable_ids
            if all_frontend and fully_writable:
                return self.frontend_implementation_agent
            return self.implementation_agent
        frontend_ids = {
            str(row.get("module_id", ""))
            for report in reports
            for row in report.writable_targets
            if isinstance(row, dict)
            and str(row.get("kind", "")).upper()
            in {"PAGE", "COMPONENT", "LAYOUT", "STORE"}
        }
        target_kinds = {
            str(row.get("kind", "")).upper()
            for report in reports
            for row in report.writable_targets
            if isinstance(row, dict) and str(row.get("kind", ""))
        }
        # A mixed frontend/backend failure must stay with the general agent;
        # the frontend specialist is only appropriate when every localized
        # writable target is frontend-owned.
        if frontend_ids and target_kinds <= {"PAGE", "COMPONENT", "LAYOUT", "STORE"}:
            return self.frontend_implementation_agent
        return self.implementation_agent

    def _implementation_agents_for_reports(
        self,
        reports: list[TestFailureReport],
        *,
        layer: str | None,
    ) -> list[ImplementationAgent]:
        """Choose the specialist sequence for the current test layer."""

        normalized_layer = str(layer or "").upper()
        if normalized_layer in {"UNIT", "INTEGRATION"}:
            return [self.implementation_agent]
        if normalized_layer != "E2E":
            return [self._implementation_agent_for_reports(reports)]

        route = classify_e2e_route(
            reports,
            code_binding_registry=self.code_binding_registry,
        )
        self._log.info(
            "E2E_REPAIR_ROUTE "
            f"route={route.route} backend_targets={list(route.backend_targets)} "
            f"frontend_targets={list(route.frontend_targets)} reasons={list(route.reasons)}"
        )
        if route.route == "FRONTEND_ONLY":
            return [self.frontend_implementation_agent]
        if route.route == "BACKEND_ONLY":
            return [self.implementation_agent]
        # CROSS_LAYER and AMBIGUOUS deliberately use two small calls.  The
        # second call receives a fresh E2E report after the first patch, so it
        # never needs a combined oversized frontend/backend payload.
        return [self.implementation_agent, self.frontend_implementation_agent]

    def _repair_mode(self, layer: str | None, agent: ImplementationAgent) -> str:
        normalized_layer = str(layer or "").upper()
        if normalized_layer == "UNIT":
            return "UNIT_REPAIR"
        if normalized_layer == "INTEGRATION":
            return "INTEGRATION_REPAIR"
        if normalized_layer == "E2E":
            return (
                "E2E_FRONTEND_REPAIR"
                if agent is self.frontend_implementation_agent
                else "E2E_BACKEND_REPAIR"
            )
        return "TDD"

    def run_node(self, requirement_id: str) -> NodeTDDResult:
        """Generate this node's tests and drive only this node to acceptance."""

        requirement_id = str(requirement_id).strip()
        result = NodeTDDResult(requirement_id=requirement_id, status="INTERNAL_ERROR")
        try:
            if self.node_states.get(requirement_id) == "NODE_ACCEPTED":
                return copy.deepcopy(self._accepted_results[requirement_id])
            error = self._precondition_error(requirement_id)
            if error:
                state = (
                    "BLOCKED_DEPENDENCY"
                    if error.startswith("ARC4542")
                    else "INTERNAL_ERROR"
                )
                return self._finish(result, state, [error])

            self._transition(requirement_id, "NODE_DISCOVERED")
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
                return self._finish(
                    result,
                    "BLOCKED_TEST_MATERIALIZATION",
                    generation.errors,
                )
            self.test_manifest = generation.manifest
            self._transition(requirement_id, "TESTS_GENERATED")
            self._transition(requirement_id, "TESTS_FROZEN")
            # The skeleton is the initial known-compilable checkpoint. Later
            # implementation rounds replace it after workspace typecheck passes.
            self._node_checkpoints[requirement_id] = _CompilableCheckpoint(
                sources=self.file_patcher.snapshot(self._checkpoint_files(requirement_id))
            )

            changed_files: set[str] = set()
            previous_patch_metadata: dict[str, Any] | None = None
            available_layers = [
                layer
                for layer in TEST_LAYERS
                if any(
                    isinstance(row, dict)
                    and str(row.get("requirement_id", "")) == requirement_id
                    and str(row.get("layer", "")).upper() == layer
                    for row in (self.test_manifest or {}).get("files", [])
                )
            ]
            if not available_layers:
                return self._finish(
                    result,
                    "BLOCKED_TEST_MATERIALIZATION",
                    [f"ARC4503 TEST_SELECTION_INVALID: no test layers for {requirement_id}."],
                )
            for skipped_layer in TEST_LAYERS:
                if skipped_layer not in available_layers:
                    self._log.info(
                        f"LAYER_GATE_SKIPPED requirement={requirement_id} "
                        f"layer={skipped_layer} reason=NO_FROZEN_TESTS"
                    )

            failure_analysis_text = ""
            functional_iterations = 0
            visual_iterations = 0
            patch_iteration = 0
            initial_changed: set[str] = set()
            retry_feedback: tuple[str, ...] = ()
            any_red_observed = False
            layer_outcomes = self._layer_gate_outcomes.setdefault(requirement_id, {})

            for layer_index, active_layer in enumerate(available_layers):
                if layer_index == 0:
                    initial_changed = self._implement_before_first_test(requirement_id)
                    changed_files.update(initial_changed)
                    result.changed_files = sorted(changed_files)
                    patch_iteration = 1 if initial_changed else 0
                self._log.info(
                    f"LAYER_GATE_STARTED requirement={requirement_id} layer={active_layer}"
                )
                baseline = self._run_and_analyze(
                    requirement_id,
                    iteration=patch_iteration,
                    include_typecheck=bool(initial_changed) and layer_index == 0,
                    layers=(active_layer,),
                    changed_files=sorted(changed_files),
                )
                self._record_compilable_checkpoint(
                    requirement_id, baseline.test_run, changed_files
                )
                result.infrastructure_retries += baseline.infrastructure_retries
                if baseline.analysis.errors:
                    return self._finish(result, "INTERNAL_ERROR", baseline.analysis.errors)
                if baseline.test_run.ok:
                    if not initial_changed and layer_index == 0:
                        self._log.info(
                            f"LAYER_GREEN_ALREADY requirement={requirement_id} "
                            f"layer={active_layer}; continuing layered gate"
                        )
                    self._transition(requirement_id, f"{active_layer}_GREEN")
                    layer_outcomes[active_layer] = "PASSED"
                    result.layer_outcomes = dict(layer_outcomes)
                    self._log.info(
                        f"LAYER_GATE_PASSED requirement={requirement_id} layer={active_layer}"
                    )
                    continue

                blocked = _blocked_state(baseline.analysis.reports)
                if blocked:
                    return self._finish(
                        result, blocked, _report_messages(baseline.analysis.reports),
                        iterations=functional_iterations,
                        visual_iterations=visual_iterations,
                        changed_files=changed_files,
                    )
                any_red_observed = True
                self._transition(requirement_id, "RED_CONFIRMED")
                reports = baseline.analysis.reports
                failure_analysis_text = baseline.analysis.agent_context
                last_fingerprint = _selected_cluster(reports)[0].failure_fingerprint
                unchanged_failures = 0
                layer_start_iterations = functional_iterations
                layer_done = False
                agent_attempt_index = 0
                no_progress_warning_emitted = False

                while not layer_done:
                    cluster = _selected_cluster(reports)
                    if functional_iterations - layer_start_iterations >= self.policy.max_iterations_per_node:
                        self._log.info(
                            f"LAYER_GATE_BUDGET_EXHAUSTED requirement={requirement_id} "
                            f"layer={active_layer} budget={self.policy.max_iterations_per_node}; "
                            "dependent layers will be skipped"
                        )
                        layer_outcomes[active_layer] = "BUDGET_EXHAUSTED"
                        result.layer_outcomes = dict(layer_outcomes)
                        break
                    functional_iterations += 1
                    patch_iteration += 1
                    result.iterations = functional_iterations
                    result.visual_iterations = visual_iterations
                    self._transition(requirement_id, "IMPLEMENTING")
                    self._log.info(
                        f"IMPLEMENTATION_PHASE requirement={requirement_id} phase=TDD_REPAIR "
                        f"layer={active_layer} iteration={patch_iteration}"
                    )

                    agents = self._implementation_agents_for_reports(
                        cluster, layer=active_layer
                    )
                    implementation_agent = agents[agent_attempt_index % len(agents)]
                    agent_attempt_index += 1
                    implementation = implementation_agent.implement(
                        ImplementationRequest(
                            requirement_id=requirement_id,
                            requirement=self.requirement_ir["nodes"][requirement_id],
                            requirement_contract=self._requirement_contract(requirement_id),
                            test_manifest=self.test_manifest or {},
                            code_binding_registry=self.code_binding_registry,
                            failure_reports=tuple(cluster),
                            failure_analysis_text=failure_analysis_text,
                            iteration=patch_iteration,
                            mode=self._repair_mode(active_layer, implementation_agent),
                            design_context=self._design_context(requirement_id),
                            previous_patch_metadata=previous_patch_metadata,
                            retry_feedback=retry_feedback,
                        )
                    )
                    if not implementation.ok or implementation.patch is None:
                        self._log.info(
                            f"LAYER_GATE_AGENT_FAILED requirement={requirement_id} "
                            f"layer={active_layer}; dependent layers will be skipped"
                        )
                        layer_outcomes[active_layer] = "AGENT_FAILED"
                        result.layer_outcomes = dict(layer_outcomes)
                        break

                    applied = self.file_patcher.apply(
                        implementation.patch,
                        code_binding_registry=self.code_binding_registry,
                    )
                    if not applied.ok:
                        retry_feedback = tuple(
                            [
                                "The previous exact patch could not be applied to the current source.",
                                *applied.rejected_changes,
                            ]
                        )
                        failure_analysis_text = "\n\n".join(
                            value
                            for value in (
                                failure_analysis_text,
                                "IMPLEMENTATION PATCH RETRY FEEDBACK:\n"
                                + "\n".join(retry_feedback),
                            )
                            if value
                        )
                        previous_patch_metadata = {
                            "iteration": patch_iteration,
                            "patch_rejected": True,
                            "rejection_feedback": applied.rejected_changes,
                        }
                        continue
                    changed_files.update(
                        _normalize_path(value) for value in applied.changed_files
                    )
                    result.changed_files = sorted(changed_files)
                    previous_patch_metadata = {
                        "iteration": patch_iteration,
                        "changed_files": applied.changed_files,
                        "changed_modules": applied.changed_modules,
                        "failure_fingerprint_before": cluster[0].failure_fingerprint,
                    }
                    if active_layer == "INTEGRATION" and "UNIT" in available_layers:
                        unit_regression = self._run_and_analyze(
                            requirement_id,
                            iteration=patch_iteration,
                            include_typecheck=True,
                            layers=("UNIT",),
                            changed_files=sorted(changed_files),
                        )
                        result.infrastructure_retries += unit_regression.infrastructure_retries
                        if not unit_regression.test_run.ok and not unit_regression.analysis.errors:
                            self._log.info(
                                f"INTEGRATION_UNIT_REGRESSION_FAILED requirement={requirement_id}"
                            )
                            reports = unit_regression.analysis.reports
                            failure_analysis_text = unit_regression.analysis.agent_context
                            if reports:
                                continue
                    if active_layer == "E2E" and implementation_agent is self.implementation_agent:
                        backend_layers = tuple(
                            layer for layer in ("UNIT", "INTEGRATION")
                            if layer in available_layers
                        )
                        if backend_layers:
                            backend_regression = self._run_and_analyze(
                                requirement_id,
                                iteration=patch_iteration,
                                include_typecheck=True,
                                layers=backend_layers,
                                changed_files=sorted(changed_files),
                            )
                            result.infrastructure_retries += backend_regression.infrastructure_retries
                            if not backend_regression.test_run.ok and not backend_regression.analysis.errors:
                                self._log.info(
                                    f"E2E_BACKEND_REGRESSION_FAILED requirement={requirement_id} "
                                    f"layers={backend_layers}"
                                )
                                reports = backend_regression.analysis.reports
                                failure_analysis_text = backend_regression.analysis.agent_context
                                if reports:
                                    continue

                    verification = self._run_and_analyze(
                        requirement_id,
                        iteration=patch_iteration,
                        include_typecheck=True,
                        layers=(active_layer,),
                        changed_files=sorted(changed_files),
                    )
                    self._record_compilable_checkpoint(
                        requirement_id, verification.test_run, changed_files
                    )
                    result.infrastructure_retries += verification.infrastructure_retries
                    if verification.analysis.errors:
                        return self._finish(
                            result, "INTERNAL_ERROR", verification.analysis.errors,
                            iterations=functional_iterations,
                            visual_iterations=visual_iterations,
                            changed_files=changed_files,
                        )
                    if verification.test_run.ok:
                        self._transition(requirement_id, f"{active_layer}_GREEN")
                        layer_outcomes[active_layer] = "PASSED"
                        result.layer_outcomes = dict(layer_outcomes)
                        layer_done = True
                        continue
                    blocked = _blocked_state(verification.analysis.reports)
                    if blocked:
                        return self._finish(
                            result, blocked, _report_messages(verification.analysis.reports),
                            iterations=functional_iterations,
                            visual_iterations=visual_iterations,
                            changed_files=changed_files,
                        )
                    reports = verification.analysis.reports
                    failure_analysis_text = verification.analysis.agent_context
                    fingerprint = _selected_cluster(reports)[0].failure_fingerprint
                    if previous_patch_metadata is not None:
                        previous_patch_metadata["failure_fingerprint_after"] = fingerprint
                        previous_patch_metadata["failure_changed"] = fingerprint != last_fingerprint
                    unchanged_failures = unchanged_failures + 1 if fingerprint == last_fingerprint else 0
                    last_fingerprint = fingerprint
                    if unchanged_failures >= self.policy.no_progress_limit:
                        if not no_progress_warning_emitted:
                            self._log.info(
                                f"LAYER_GATE_NO_PROGRESS requirement={requirement_id} "
                                f"layer={active_layer} unchanged={unchanged_failures}; "
                                "stopping this layer and skipping dependent layers"
                            )
                            no_progress_warning_emitted = True
                        layer_outcomes[active_layer] = "NO_PROGRESS"
                        result.layer_outcomes = dict(layer_outcomes)
                        break

                if layer_outcomes.get(active_layer) != "PASSED":
                    for skipped_layer in available_layers[layer_index + 1 :]:
                        layer_outcomes[skipped_layer] = (
                            f"SKIPPED_BLOCKED_BY_{active_layer}"
                        )
                        self._log.info(
                            f"LAYER_GATE_SKIPPED requirement={requirement_id} "
                            f"layer={skipped_layer} reason=PREREQUISITE_{active_layer}_FAILED"
                        )
                    result.layer_outcomes = dict(layer_outcomes)
                    break

            if not any_red_observed and not initial_changed:
                self._log.info(
                    f"LAYER_GREEN_ALREADY requirement={requirement_id} "
                    "all available layers passed without a repair patch"
                )
            result.layer_outcomes = dict(layer_outcomes)
            incomplete_layers = {
                layer: layer_outcomes.get(layer, "NOT_RUN")
                for layer in available_layers
                if layer_outcomes.get(layer) != "PASSED"
            }
            if incomplete_layers:
                warnings = [
                    "LAYER_GATE_INCOMPLETE: "
                    f"{layer} ended with {outcome}; the latest compilable checkpoint "
                    "was preserved and dependent later layers were not executed."
                    for layer, outcome in incomplete_layers.items()
                ]
                return self._finish(
                    result,
                    "NODE_COMPLETED_WITH_FAILURES",
                    warnings,
                    iterations=functional_iterations,
                    visual_iterations=visual_iterations,
                    changed_files=changed_files,
                )
            return self._accept_node(result)
        except Exception as exc:
            return self._finish(
                result,
                "INTERNAL_ERROR",
                [f"ARC4540 NODE_TDD_INTERNAL_ERROR: {type(exc).__name__}: {exc}"],
                iterations=result.iterations,
                visual_iterations=result.visual_iterations,
                changed_files=result.changed_files,
            )

    def run_aggregate_node(self, requirement_id: str) -> NodeTDDResult:
        """Implement targets owned by a non-leaf requirement without generating tests."""

        requirement_id = str(requirement_id).strip()
        result = NodeTDDResult(requirement_id=requirement_id, status="INTERNAL_ERROR")
        try:
            if self.node_states.get(requirement_id) in {
                "NODE_ACCEPTED",
                "AGGREGATE_ACCEPTED",
                "NO_IMPLEMENTATION_REQUIRED",
            }:
                return copy.deepcopy(self._accepted_results[requirement_id])
            nodes = self.requirement_ir.get("nodes", {})
            requirement = nodes.get(requirement_id) if isinstance(nodes, dict) else None
            if not isinstance(requirement, dict) or requirement.get("type") != "FOLDER":
                return self._finish(
                    result,
                    "INTERNAL_ERROR",
                    [f"ARC4541 AGGREGATE_INPUT_INVALID: {requirement_id} is not a folder requirement."],
                )

            self._transition(requirement_id, "AGGREGATE_DISCOVERED")
            resolved = CodeTargetResolver(
                self.code_binding_registry
            ).resolve_requirement_targets(requirement_id)
            writable_targets = [
                copy.deepcopy(row)
                for row in resolved.get("owned_targets", [])
                if isinstance(row, dict)
            ]
            frontend_kinds = {"PAGE", "COMPONENT", "LAYOUT", "STORE"}
            frontend_targets = [
                row for row in writable_targets
                if str(row.get("kind", "")).upper() in frontend_kinds
            ]
            ignored_backend_targets = [
                str(row.get("module_id", ""))
                for row in writable_targets
                if str(row.get("kind", "")).upper() not in frontend_kinds
            ]
            if ignored_backend_targets:
                self._log.info(
                    f"AGGREGATE_BACKEND_TARGETS_DEFERRED requirement={requirement_id} "
                    f"targets={sorted(ignored_backend_targets)}"
                )
            if not frontend_targets:
                self._transition(requirement_id, "NO_IMPLEMENTATION_REQUIRED")
                return self._finish(result, "NO_IMPLEMENTATION_REQUIRED", [])

            self._node_checkpoints[requirement_id] = _CompilableCheckpoint(
                sources=self.file_patcher.snapshot(self._checkpoint_files(requirement_id))
            )

            self._transition(requirement_id, "AGGREGATE_FRONTEND_IMPLEMENTING")
            changed, errors, batch_count, remaining_targets = self._implement_frontend_aggregate_batches(
                requirement_id,
                frontend_targets,
                requirement,
            )
            result.incomplete_targets = list(remaining_targets)
            if errors:
                return self._finish(
                    result,
                    "AGGREGATE_IMPLEMENTATION_INCOMPLETE",
                    errors,
                    iterations=batch_count,
                    changed_files=changed,
                )
            verification_errors = self._verify_aggregate_patch(
                requirement_id,
                changed,
            )
            if verification_errors:
                return self._finish(
                    result,
                    "AGGREGATE_IMPLEMENTATION_INCOMPLETE",
                    verification_errors,
                    iterations=batch_count,
                    changed_files=changed,
                )
            self._transition(requirement_id, "AGGREGATE_TYPECHECKED")
            self._transition(requirement_id, "AGGREGATE_REGRESSION_CHECKED")
            self._transition(requirement_id, "AGGREGATE_ACCEPTED")
            return self._finish(
                result,
                "AGGREGATE_ACCEPTED",
                [],
                iterations=batch_count,
                changed_files=changed,
            )

        except Exception as exc:
            return self._finish(
                result,
                "INTERNAL_ERROR",
                [f"ARC4540 AGGREGATE_IMPLEMENTATION_INTERNAL_ERROR: {type(exc).__name__}: {exc}"],
                iterations=result.iterations,
                changed_files=result.changed_files,
            )

    def _implement_frontend_aggregate_batches(
        self,
        requirement_id: str,
        writable_targets: list[dict[str, Any]],
        requirement: dict[str, Any],
    ) -> tuple[set[str], list[str], int, list[str]]:
        """Implement aggregate frontend targets one module per model call.

        Aggregate requirements do not have a failing test to narrow the scope,
        so sending several full JSX regions to one response is especially
        risky.  A single module per call keeps the response small and makes a
        truncated response affect only the current module.
        """

        remaining = {
            str(row.get("module_id", ""))
            for row in writable_targets
            if str(row.get("module_id", "")).strip()
        }
        changed_files: set[str] = set()
        batch = 0
        model_attempts = 0
        errors: list[str] = []
        while remaining:
            if model_attempts >= self.policy.max_iterations_per_node:
                errors.append(
                    "ARC4547 AGGREGATE_ITERATION_BUDGET_EXHAUSTED: aggregate frontend "
                    f"implementation budget was exhausted after {model_attempts} model "
                    f"attempt(s); remaining={sorted(remaining)}."
                )
                break
            batch += 1
            resolved = CodeTargetResolver(
                self.code_binding_registry
            ).resolve_requirement_targets(requirement_id)
            targets = [
                row
                for row in resolved.get("owned_targets", [])
                if isinstance(row, dict)
                and str(row.get("kind", "")).upper()
                in {"PAGE", "COMPONENT", "LAYOUT", "STORE"}
                and str(row.get("module_id", "")) in remaining
            ]
            if not targets:
                errors.append(
                    "ARC4544 FRONTEND_AGGREGATE_STALLED: no remaining writable frontend "
                    "target could be resolved."
                )
                break
            target = targets[0]
            module_id = str(target["module_id"])
            target_attempt = 0
            retry_feedback: list[str] = []
            target_completed = False
            while target_attempt <= self.policy.initial_target_retry_count:
                if model_attempts >= self.policy.max_iterations_per_node:
                    retry_feedback = [
                        "ARC4547 AGGREGATE_ITERATION_BUDGET_EXHAUSTED: no further "
                        "aggregate model attempt is available for this target."
                    ]
                    break
                target_attempt += 1
                model_attempts += 1
                self._log.info(
                    f"FRONTEND_AGGREGATE_BATCH requirement={requirement_id} "
                    f"batch={batch} module={module_id} target_attempt={target_attempt} "
                    f"remaining={len(remaining)}"
                )
                fingerprint = hashlib.sha256(
                    f"{requirement_id}:aggregate-frontend:{batch}:{module_id}".encode(
                        "utf-8"
                    )
                ).hexdigest()
                report = TestFailureReport(
                    requirement_id=requirement_id,
                    iteration=batch,
                    test_id=f"aggregate:{requirement_id}",
                    test_ids=[f"aggregate:{requirement_id}"],
                    layer="AGGREGATE",
                    phase="FRONTEND_BOOTSTRAP",
                    failure_class="IMPLEMENTATION_BEHAVIOR",
                    message="Implement this one frontend module in the aggregate requirement.",
                    stack_frames=[],
                    target_modules=[module_id],
                    writable_targets=[copy.deepcopy(target)],
                    read_only_dependencies=[],
                    changed_files=sorted(changed_files),
                    failure_fingerprint=fingerprint,
                    diagnostic_output=(
                        "Implement only the supplied frontend module. Preserve the declared "
                        "routes, API clients, stores, and visual direction; return exact "
                        "search/replacement edits for this module.\n"
                        + ("RETRY FEEDBACK:\n" + "\n".join(retry_feedback) if retry_feedback else "")
                    ),
                )
                implementation = self.frontend_implementation_agent.implement(
                    ImplementationRequest(
                        requirement_id=requirement_id,
                        requirement=requirement,
                        requirement_contract={},
                        test_manifest={},
                        code_binding_registry=self.code_binding_registry,
                        failure_reports=(report,),
                        iteration=batch,
                        mode="AGGREGATE",
                        design_context=self._design_context(requirement_id),
                        retry_feedback=tuple(retry_feedback),
                        target_module_ids=(module_id,),
                    )
                )
                if not implementation.ok or implementation.patch is None:
                    retry_feedback = list(
                        implementation.errors
                        or [f"ARC4544 IMPLEMENTATION_AGENT_FAILED: {implementation.status}."]
                    )
                    continue
                applied = self.file_patcher.apply(
                    implementation.patch,
                    code_binding_registry=self.code_binding_registry,
                )
                if not applied.ok:
                    retry_feedback = list(applied.rejected_changes)
                    continue
                if module_id not in set(applied.changed_modules):
                    checkpoint = self._node_checkpoints.get(requirement_id)
                    if checkpoint is not None:
                        self.file_patcher.restore(checkpoint.sources)
                        changed_files.clear()
                        changed_files.update(checkpoint.changed_files)
                    retry_feedback = [
                        "ARC4544 FRONTEND_AGGREGATE_STALLED: the patch did not change the "
                        "current frontend module."
                    ]
                    continue

                # Every aggregate edit is compile-gated independently.  If the
                # current batch breaks typecheck, restore the previous aggregate
                # checkpoint and retry only this target; earlier compilable
                # batches remain intact.
                typecheck = self.test_runner.run_workspace_typecheck()
                if typecheck.status != "PASSED":
                    checkpoint = self._node_checkpoints.get(requirement_id)
                    if checkpoint is not None:
                        self.file_patcher.restore(checkpoint.sources)
                        changed_files.clear()
                        changed_files.update(checkpoint.changed_files)
                    detail = (
                        typecheck.stderr
                        or typecheck.stdout
                        or typecheck.error
                        or "workspace typecheck failed"
                    )
                    retry_feedback = [
                        "The aggregate patch was applied but failed workspace typecheck; "
                        "repair only the current frontend target using the latest source.",
                        str(detail)[-4000:],
                    ]
                    continue

                # A source file may bind several frontend modules. Completing
                # one target must not silently mark every sibling in that file
                # complete merely because the file hash changed.
                remaining.discard(module_id)
                changed_files.update(_normalize_path(value) for value in applied.changed_files)
                self._node_checkpoints[requirement_id] = _CompilableCheckpoint(
                    sources=self.file_patcher.snapshot(self._checkpoint_files(requirement_id)),
                    changed_files=sorted(changed_files),
                )
                self._log.info(
                    f"ARC4554 AGGREGATE_COMPILE_CHECKPOINT_UPDATED requirement={requirement_id} "
                    f"module={module_id} changed_files={len(changed_files)}"
                )
                target_completed = True
                break
            if not target_completed:
                errors.extend(retry_feedback)
                break
        return changed_files, errors, model_attempts, sorted(remaining)

    def _verify_aggregate_patch(
        self,
        requirement_id: str,
        changed_files: set[str],
    ) -> list[str]:
        """Apply the same post-patch gates to aggregate and atomic edits."""

        typecheck = self.test_runner.run_workspace_typecheck()
        if typecheck.status != "PASSED":
            detail = typecheck.stderr or typecheck.stdout or typecheck.error or "unknown failure"
            return [
                "ARC4549 AGGREGATE_TYPECHECK_FAILED: full workspace typecheck failed: "
                + detail[-4000:]
            ]
        self._record_compilable_checkpoint(
            requirement_id,
            TestRunResult(
                requirement_id=requirement_id,
                status="PASSED",
                selected_layers=[],
                selected_test_ids=[],
                selected_files=[],
                commands=[typecheck],
            ),
            changed_files,
        )
        test_requirement_ids = {
            str(row.get("requirement_id", ""))
            for row in (self.test_manifest or {}).get("requirements", [])
            if isinstance(row, dict)
        }
        for accepted_id in sorted(set(self._accepted_results) & test_requirement_ids):
            test_run = self.test_runner.run(
                TestSelection(
                    requirement_id=accepted_id,
                    include_typecheck=False,
                    stop_on_failure=True,
                ),
                test_manifest=self.test_manifest,
                environment_manifest=self.environment_manifest,
            )
            if not test_run.ok:
                command_detail = next(
                    (
                        command.stderr or command.stdout or command.error
                        for command in reversed(test_run.commands)
                        if command.status != "PASSED"
                    ),
                    None,
                )
                return [
                    f"ARC4549 AGGREGATE_REGRESSION_FAILED: accepted node {accepted_id} failed"
                    + (f": {str(command_detail)[-4000:]}" if command_detail else ".")
                ]
        return []

    def _restore_accepted_checkpoints(self) -> None:
        nodes = self.requirement_ir.get("nodes", {})
        if not isinstance(nodes, dict):
            return
        frozen_requirements = {
            str(row.get("requirement_id", ""))
            for row in (self.test_manifest or {}).get("requirements", [])
            if isinstance(row, dict) and row.get("state") == "TESTS_FROZEN"
        }
        for requirement_id in nodes:
            node = nodes.get(requirement_id, {})
            if (
                isinstance(node, dict)
                and node.get("type") == "ATOMIC"
                and str(requirement_id) not in frozen_requirements
            ):
                continue
            path = self._node_root(str(requirement_id)) / "result.json"
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                not isinstance(payload, dict)
                or payload.get("status") != "NODE_ACCEPTED"
                or payload.get("errors")
                or str(payload.get("requirement_id", "")) != str(requirement_id)
            ):
                continue
            result = NodeTDDResult(
                requirement_id=str(requirement_id),
                status="NODE_ACCEPTED",
                iterations=int(payload.get("iterations", 0)),
                visual_iterations=int(payload.get("visual_iterations", 0)),
                infrastructure_retries=int(payload.get("infrastructure_retries", 0)),
                state_history=[str(value) for value in payload.get("state_history", [])],
                changed_files=[str(value) for value in payload.get("changed_files", [])],
                impacted_requirements=[str(value) for value in payload.get("impacted_requirements", [])],
                artifacts={"result": str(path)},
            )
            self.node_states[str(requirement_id)] = "NODE_ACCEPTED"
            self._state_history[str(requirement_id)] = list(result.state_history) or ["NODE_ACCEPTED"]
            self._accepted_results[str(requirement_id)] = result

    def _implement_before_first_test(self, requirement_id: str) -> set[str]:
        """Make the TDD loop's first implementation calls without test feedback."""

        try:
            resolved = CodeTargetResolver(
                self.code_binding_registry
            ).resolve_requirement_targets(requirement_id)
        except (KeyError, ValueError) as exc:
            self._log.info(
                f"TDD_TARGETS_UNAVAILABLE requirement={requirement_id} error={exc}"
            )
            return set()

        backend_kinds = {"DB", "FUNC", "API"}
        frontend_kinds = {"STORE", "COMPONENT", "PAGE", "LAYOUT"}
        targets = [
            copy.deepcopy(row)
            for row in resolved.get("owned_targets", [])
            if isinstance(row, dict)
            and str(row.get("kind", "")).upper() in backend_kinds | frontend_kinds
            and str(row.get("module_id", "")).strip()
        ]
        kind_priority = {
            "DB": 0,
            "FUNC": 1,
            "API": 2,
            "STORE": 3,
            "COMPONENT": 4,
            "PAGE": 5,
            "LAYOUT": 6,
        }
        targets.sort(
            key=lambda row: (
                kind_priority.get(str(row.get("kind", "")).upper(), 99),
                str(row.get("module_id", "")),
            )
        )
        changed_files: set[str] = set()
        requirement = self.requirement_ir.get("nodes", {}).get(requirement_id, {})
        if not isinstance(requirement, dict):
            requirement = {}
        for target in targets:
            module_id = str(target.get("module_id", "")).strip()
            kind = str(target.get("kind", "")).upper()
            agent = (
                self.frontend_implementation_agent
                if kind in frontend_kinds
                else self.implementation_agent
            )
            implementation = agent.implement(
                ImplementationRequest(
                    requirement_id=requirement_id,
                    requirement=requirement,
                    requirement_contract=self._requirement_contract(requirement_id),
                    test_manifest=self.test_manifest or {},
                    code_binding_registry=self.code_binding_registry,
                    failure_reports=(),
                    failure_analysis_text="",
                    iteration=1,
                    mode="TDD",
                    design_context=self._design_context(requirement_id),
                    target_module_ids=(module_id,),
                )
            )
            if not implementation.ok or implementation.patch is None:
                self._log.info(
                    f"TDD_FIRST_IMPLEMENTATION_FAILED requirement={requirement_id} "
                    f"target={module_id} errors={implementation.errors}"
                )
                continue
            applied = self.file_patcher.apply(
                implementation.patch,
                code_binding_registry=self.code_binding_registry,
            )
            if not applied.ok:
                self._log.info(
                    f"TDD_FIRST_PATCH_REJECTED requirement={requirement_id} "
                    f"target={module_id} errors={applied.rejected_changes}"
                )
                continue
            changed_files.update(
                _normalize_path(value) for value in applied.changed_files
            )

        return changed_files

    def _run_and_analyze(
        self,
        requirement_id: str,
        *,
        iteration: int,
        include_typecheck: bool,
        layers: tuple[str, ...] = (),
        changed_files: list[str],
    ) -> _RunAnalysis:
        retries = 0
        while True:
            test_run = self.test_runner.run(
                TestSelection(
                    requirement_id=requirement_id,
                    layers=layers,
                    include_typecheck=include_typecheck,
                    stop_on_failure=False,
                ),
                test_manifest=self.test_manifest,
                environment_manifest=self.environment_manifest,
            )
            analysis = self.failure_analyzer.classify(
                test_run,
                code_binding_registry=self.code_binding_registry,
                test_manifest=self.test_manifest or {},
                iteration=iteration,
                changed_files=changed_files,
            )
            self._write_iteration_diagnostics(
                requirement_id=requirement_id,
                iteration=iteration,
                retry=retries,
                test_run=test_run,
                analysis=analysis,
            )
            has_infrastructure_failure = any(
                report.failure_class == "INFRASTRUCTURE"
                for report in analysis.reports
            )
            if (
                test_run.ok
                or analysis.errors
                or not has_infrastructure_failure
                or retries >= self.policy.infra_retry_count
            ):
                return _RunAnalysis(test_run, analysis, retries)
            retries += 1

    def _record_compilable_checkpoint(
        self,
        requirement_id: str,
        test_run: TestRunResult,
        changed_files: set[str] | list[str],
    ) -> None:
        """Keep the newest source state whose workspace typecheck passed."""

        typecheck_passed = any(
            str(command.phase).upper() == "TYPECHECK"
            and command.status == "PASSED"
            for command in test_run.commands
        )
        if not typecheck_passed:
            return
        self._node_checkpoints[requirement_id] = _CompilableCheckpoint(
            sources=self.file_patcher.snapshot(self._checkpoint_files(requirement_id)),
            changed_files=sorted({_normalize_path(value) for value in changed_files}),
        )
        self._log.info(
            f"ARC4554 COMPILE_CHECKPOINT_UPDATED requirement={requirement_id} "
            f"changed_files={len(changed_files)}"
        )

    def _write_iteration_diagnostics(
        self,
        *,
        requirement_id: str,
        iteration: int,
        retry: int,
        test_run: TestRunResult,
        analysis: FailureAnalysisResult,
    ) -> None:
        """Persist exact runner feedback before it is summarized for control flow."""

        diagnostics_root = self._node_root(requirement_id) / "iterations"
        stem = f"iteration-{max(0, int(iteration)):03d}-retry-{max(0, int(retry)):02d}"
        write_json_atomic(diagnostics_root / f"{stem}-test-run.json", test_run.to_dict())
        write_json_atomic(
            diagnostics_root / f"{stem}-failure-analysis.json",
            analysis.to_dict(),
        )

    def _accept_node(self, result: NodeTDDResult) -> NodeTDDResult:
        requirement_id = result.requirement_id
        available_layers = {
            str(row.get("layer", "")).upper()
            for row in (self.test_manifest or {}).get("files", [])
            if isinstance(row, dict)
            and str(row.get("requirement_id", "")) == requirement_id
        }
        layer_outcomes = self._layer_gate_outcomes.get(requirement_id)
        if layer_outcomes is None:
            for layer in TEST_LAYERS:
                if layer in available_layers:
                    self._transition(requirement_id, f"{layer}_GREEN")
        else:
            for layer in TEST_LAYERS:
                if layer_outcomes.get(layer) == "PASSED":
                    self._transition(requirement_id, f"{layer}_GREEN")

        impacted = self._impacted_accepted_requirements(
            requirement_id,
            set(result.changed_files),
        )
        result.impacted_requirements = impacted
        for impacted_id in impacted:
            test_run = self.test_runner.run(
                TestSelection(
                    requirement_id=impacted_id,
                    include_typecheck=False,
                    stop_on_failure=True,
                ),
                test_manifest=self.test_manifest,
                environment_manifest=self.environment_manifest,
            )
            if not test_run.ok:
                return self._finish(
                    result,
                    "REGRESSION_FAILED",
                    test_run.errors
                    or [f"ARC4546 REGRESSION_FAILED: accepted node {impacted_id} failed."],
                )

        self._transition(requirement_id, "NODE_ACCEPTED")
        return self._finish(result, "NODE_ACCEPTED", [])

    def _precondition_error(self, requirement_id: str) -> str | None:
        nodes = self.requirement_ir.get("nodes")
        if not requirement_id:
            return "ARC4541 NODE_TDD_INPUT_INVALID: requirement_id is required."
        if not isinstance(nodes, dict) or requirement_id not in nodes:
            return f"ARC4541 NODE_TDD_INPUT_INVALID: unknown requirement {requirement_id}."
        dependencies = self.dependency_graph.get("atomic_dependencies", {}).get(
            requirement_id, []
        )
        unprocessed = [
            str(value)
            for value in dependencies
            if self.node_states.get(str(value)) not in TERMINAL_NODE_STATES
        ]
        if unprocessed:
            return (
                f"ARC4542 NODE_TDD_DEPENDENCY_BLOCKED: {requirement_id} requires "
                f"previously processed nodes {sorted(unprocessed)}."
            )
        return None

    def _requirement_contract(self, requirement_id: str) -> dict[str, Any]:
        return next(
            (
                copy.deepcopy(row.get("contract", {}))
                for row in self.design_ir.get("requirements", [])
                if isinstance(row, dict) and str(row.get("id", "")) == requirement_id
            ),
            {},
        )

    def _design_context(self, requirement_id: str) -> dict[str, Any]:
        resolved = CodeTargetResolver(
            self.code_binding_registry
        ).resolve_requirement_targets(requirement_id)
        all_targets = {
            str(row.get("module_id", "")): row
            for key in ("owned_targets", "dependency_targets")
            for row in resolved.get(key, [])
            if isinstance(row, dict) and str(row.get("module_id", ""))
        }
        owned_ids = {
            str(row.get("module_id", ""))
            for row in resolved.get("owned_targets", [])
            if isinstance(row, dict) and str(row.get("module_id", ""))
        }
        screens = [
            copy.deepcopy(row)
            for row in self.frontend_ir.get("screens", [])
            if isinstance(row, dict)
            and (
                str(row.get("id", "")) in owned_ids
                or requirement_id
                in {str(value) for value in row.get("requirement_ids", [])}
            )
        ]
        primary_screen_ids = {str(row.get("id", "")) for row in screens}
        route_index = {
            str(row.get("route", "")): row
            for row in self.frontend_ir.get("screens", [])
            if isinstance(row, dict) and str(row.get("route", ""))
        }
        navigation_routes = {
            str(target.get("target_route", ""))
            for screen in screens
            for target in screen.get("navigation_targets", [])
            if isinstance(target, dict) and str(target.get("target_route", ""))
        }
        screen_ids = set(primary_screen_ids)
        for route in sorted(navigation_routes):
            destination = route_index.get(route)
            destination_id = str((destination or {}).get("id", ""))
            if destination is not None and destination_id not in screen_ids:
                screens.append(copy.deepcopy(destination))
                screen_ids.add(destination_id)

        placements = [
            copy.deepcopy(row)
            for row in self.frontend_ir.get("placements", [])
            if isinstance(row, dict)
            and str(row.get("requirement_id", "")) == requirement_id
        ]
        shared_state_policies = [
            copy.deepcopy(row)
            for row in self.frontend_ir.get("shared_state_policies", [])
            if isinstance(row, dict)
            and (
                str(row.get("id", "")) in owned_ids
                or requirement_id
                in {str(value) for value in row.get("requirement_ids", [])}
            )
        ]
        referenced_api_ids = {
            str(value)
            for screen in screens
            for value in screen.get("required_api_ids", [])
            if str(value)
        }
        direct_dependency_ids = {
            str(value)
            for module_id in owned_ids
            for value in all_targets.get(module_id, {}).get("callees", [])
            if str(value)
        }
        direct_dependency_ids.update(referenced_api_ids)
        direct_dependency_ids.update(
            f"API_CLIENT::{api_id}" for api_id in referenced_api_ids
        )
        module_ids = owned_ids | (direct_dependency_ids & set(all_targets))

        visual_ids = {
            str(value)
            for row in screens
            for value in row.get("visual_reference_ids", [])
            if str(value)
        }
        visual_references = [
            copy.deepcopy(row)
            for row in self.frontend_ir.get("visual_references", [])
            if isinstance(row, dict) and str(row.get("id", "")) in visual_ids
        ]
        screen_components = [
            copy.deepcopy(row)
            for row in self.frontend_ir.get("screen_components", [])
            if isinstance(row, dict) and str(row.get("screen_id", "")) in screen_ids
        ]
        backend_modules = [
            copy.deepcopy(row)
            for row in self.design_ir.get("modules", [])
            if isinstance(row, dict)
            and (
                str(row.get("id", "")) in module_ids
                or str(row.get("module_id", "")) in module_ids
                or str(row.get("requirement_id", "")) == requirement_id
            )
        ]
        return {
            "requirement_id": requirement_id,
            "module_ids": sorted(module_ids),
            "backend_modules": backend_modules,
            "frontend_scope": "COMPLETE_REQUIREMENT_FRONTEND_SURFACE",
            "primary_screen_ids": sorted(primary_screen_ids),
            "owned_component_ids": sorted(
                str(row.get("id", ""))
                for row in screen_components
                if requirement_id in {str(value) for value in row.get("requirement_ids", [])}
            ),
            "frontend": {
                "screens": screens,
                "placements": placements,
                "screen_components": screen_components,
                "shared_state_policies": shared_state_policies,
                "visual_references": visual_references,
            },
        }

    def _impacted_accepted_requirements(
        self,
        current_id: str,
        changed_files: set[str],
    ) -> list[str]:
        if not changed_files:
            return []
        impacted: set[str] = set()
        for row in (self.test_manifest or {}).get("tests", []):
            if not isinstance(row, dict):
                continue
            requirement_id = str(row.get("requirement_id", ""))
            if (
                requirement_id
                and requirement_id != current_id
                and self.node_states.get(requirement_id) == "NODE_ACCEPTED"
                and changed_files
                & {_normalize_path(value) for value in row.get("target_files", [])}
            ):
                impacted.add(requirement_id)
        return sorted(impacted)

    def _budget_exhausted(
        self,
        result: NodeTDDResult,
        iterations: int,
        visual_iterations: int,
        changed_files: set[str],
        budget_name: str,
    ) -> NodeTDDResult:
        return self._finish(
            result,
            "ITERATION_BUDGET_EXHAUSTED",
            [f"ARC4547 ITERATION_BUDGET_EXHAUSTED: {budget_name} budget was exhausted."],
            iterations=iterations,
            visual_iterations=visual_iterations,
            changed_files=changed_files,
        )

    def _transition(self, requirement_id: str, state: str) -> None:
        self.node_states[requirement_id] = state
        history = self._state_history.setdefault(requirement_id, [])
        if not history or history[-1] != state:
            history.append(state)

    def _finish(
        self,
        result: NodeTDDResult,
        status: str,
        errors: list[str],
        *,
        iterations: int | None = None,
        visual_iterations: int | None = None,
        changed_files: set[str] | list[str] | None = None,
    ) -> NodeTDDResult:
        if status not in TERMINAL_NODE_STATES:
            status = "INTERNAL_ERROR"
            errors = [*errors, "ARC4540 NODE_TDD_INTERNAL_ERROR: invalid terminal state."]
        self._transition(result.requirement_id or "unknown", status)
        result.status = status
        if iterations is not None:
            result.iterations = iterations
        if visual_iterations is not None:
            result.visual_iterations = visual_iterations
        if changed_files is not None:
            result.changed_files = sorted({_normalize_path(value) for value in changed_files})
        result.state_history = list(
            self._state_history.get(result.requirement_id or "unknown", [])
        )
        result.errors = list(dict.fromkeys(str(value) for value in errors if str(value)))
        if status in {
            "NODE_ACCEPTED",
            "AGGREGATE_ACCEPTED",
            "NO_IMPLEMENTATION_REQUIRED",
        }:
            checkpoint = self._node_checkpoints.get(result.requirement_id)
            if checkpoint is not None:
                result.checkpoint_files = sorted(checkpoint.sources)
            self._node_checkpoints.pop(result.requirement_id, None)
            self._accepted_results[result.requirement_id] = copy.deepcopy(result)
        else:
            self._restore_last_compilable_checkpoint(result)
        result_path = self._node_root(result.requirement_id or "unknown") / "result.json"
        write_json_atomic(result_path, result.to_dict())
        result.artifacts["result"] = str(result_path)
        return result

    def _restore_last_compilable_checkpoint(self, result: NodeTDDResult) -> None:
        """Retain the newest typecheck-passing version after node failure."""

        checkpoint = self._node_checkpoints.pop(result.requirement_id, None)
        if checkpoint is None:
            return
        restored, errors = self.file_patcher.restore(checkpoint.sources)
        if restored:
            result.changed_files = list(checkpoint.changed_files)
            result.checkpoint_files = sorted(checkpoint.sources)
            self._log.info(
                f"ARC4554 NODE_PRESERVED_COMPILE_CHECKPOINT requirement={result.requirement_id} "
                f"status={result.status} checkpoint_files={len(restored)}"
            )
            result.errors = list(
                dict.fromkeys(
                    [
                        *result.errors,
                        "ARC4554 NODE_PRESERVED_COMPILE_CHECKPOINT: kept "
                        f"{len(restored)} file(s) at the latest workspace-typecheck-passing "
                        f"version after {result.status}; no older skeleton was restored.",
                    ]
                )
            )
        if errors:
            self._log.info(
                f"ARC4554 NODE_PRESERVE_COMPILE_CHECKPOINT_INCOMPLETE requirement={result.requirement_id} "
                f"failures={errors}"
            )
            result.errors = list(
                dict.fromkeys(
                    [
                        *result.errors,
                        f"ARC4554 NODE_PRESERVE_COMPILE_CHECKPOINT_INCOMPLETE: {errors}.",
                    ]
                )
            )

    def _checkpoint_files(self, requirement_id: str) -> list[str]:
        """List source files owned by this requirement for checkpointing only."""

        try:
            targets = CodeTargetResolver(
                self.code_binding_registry
            ).resolve_requirement_targets(requirement_id)
        except (KeyError, ValueError):
            return []
        return sorted(
            {
                str(row.get("file", ""))
                for row in targets.get("owned_targets", [])
                if isinstance(row, dict) and str(row.get("file", ""))
            }
        )

    def _node_root(self, requirement_id: str) -> Path:
        safe = re.sub(r"[^a-z0-9]+", "-", requirement_id.lower()).strip("-")
        safe = safe or "requirement"
        digest = hashlib.sha256(requirement_id.encode("utf-8")).hexdigest()[:8]
        return self._tdd_root / f"{safe}-{digest}"


def _selected_cluster(reports: list[TestFailureReport]) -> list[TestFailureReport]:
    for failure_class in ACTIONABLE_FAILURE_CLASSES:
        candidates = [row for row in reports if row.failure_class == failure_class]
        if candidates:
            fingerprint = candidates[0].failure_fingerprint
            return [row for row in candidates if row.failure_fingerprint == fingerprint]
    raise ValueError("No actionable failure cluster is available.")


def _blocked_state(reports: list[TestFailureReport]) -> str | None:
    classes = {row.failure_class for row in reports}
    if "INFRASTRUCTURE" in classes:
        return "BLOCKED_INFRA"
    if "TEST_MATERIALIZATION" in classes:
        return "BLOCKED_TEST_MATERIALIZATION"
    if "TEST_OR_CONTRACT_INCONSISTENT" in classes:
        return "BLOCKED_CONTRACT"
    # Every remaining failure ran through a module another requirement still owes.
    # Blocking here keeps the node schedulable again later instead of spending its
    # iteration budget compensating for code that does not exist yet.
    if classes == {"DEFERRED_DEPENDENCY"}:
        return "BLOCKED_DEPENDENCY"
    # A deferred dependency alongside a real defect is not itself blocking: the
    # actionable cluster is still worth one patch, and the stub hit is only noise.
    if not classes or not (classes - {"DEFERRED_DEPENDENCY"}).issubset(
        set(ACTIONABLE_FAILURE_CLASSES)
    ):
        return "BLOCKED_CONTRACT"
    return None


def _report_messages(reports: list[TestFailureReport]) -> list[str]:
    messages: list[str] = []
    classes = {row.failure_class for row in reports}
    if "TEST_MATERIALIZATION" in classes:
        messages.append(
            "COMPILER_OWNED_WARNING: test materialization failed; repair the compiler/test artifact, "
            "not application implementation."
        )
    if "INFRASTRUCTURE" in classes:
        messages.append(
            "COMPILER_OWNED_WARNING: infrastructure failed before business behavior could be evaluated; "
            "do not guess an application patch."
        )
    if "TEST_OR_CONTRACT_INCONSISTENT" in classes:
        messages.append(
            "COMPILER_OWNED_WARNING: test/contract/generated glue is inconsistent; frozen tests, imports, "
            "routes, and compiler glue remain read-only."
        )
    if "DEFERRED_DEPENDENCY" in classes:
        messages.append(
            "DEPENDENCY_SCOPE_WARNING: repair the dependency under its owning requirement before retrying."
        )
    messages.extend(row.message for row in reports if row.message)
    return list(dict.fromkeys(messages))


def _normalize_path(value: Any) -> str:
    return str(value).replace("\\", "/").strip().strip("/")


def _bounded_int(
    environment: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        return max(minimum, min(int(environment.get(name, str(default))), maximum))
    except (TypeError, ValueError):
        return default
