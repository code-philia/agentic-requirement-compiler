from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from app_type_handler import create_app_type_handler
from agents.context.pipeline import context_pipeline
from core import sessions
from core.service import get_runtime
from core.path_compat import normalize_windows_extended_prefix_text
from core.visual_analysis import analyze_and_attach_visual_references
from app_type_handler.test_results import parse_test_results


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]
TDD_RUN_TESTS_BUDGET = 10
TDD_REGRESSION_RECOVERY_CYCLES = 2
TDD_REGRESSION_RECOVERY_RUNS = 2
TEST_GENERATION_ADMISSION_ATTEMPTS = 5
ALLOWED_INTERFACE_TYPES = {"UI", "API", "FUNC", "DB"}
TDD_BATCH_ORDER = ("Unit", "Integration", "E2E")

class WorkflowPhaseRunner:
    """Run ARC DESIGN and IMPLEMENT phases using the agent adapters."""

    def __init__(
        self,
        *,
        workspace_path: str,
        requirement_path: str,
        app_type: str,
        interface_designer: Any,
        test_generator: Any,
        test_driven_developer: Any,
        log_cb: LogCallback | None = None,
    ) -> None:
        self.workspace_path = str(Path(workspace_path).expanduser().resolve())
        self.requirement_path = requirement_path
        self.app_type = app_type
        self.interface_designer = interface_designer
        self.test_generator = test_generator
        self.test_driven_developer = test_driven_developer
        self.log_cb = log_cb
        self.app_handler = create_app_type_handler(
            app_type=app_type,
            workspace_path=self.workspace_path,
            requirement_path=requirement_path,
            interface_designer=interface_designer,
            log_cb=self._log,
        )
        self.test_driven_developer.app_handler = self.app_handler

    @property
    def traceability(self):
        return get_runtime().traceability

    async def run_design_phase(self, node_id: str, requirement_data: dict[str, Any]) -> bool:
        is_non_leaf = bool(requirement_data.get("children_ids"))
        prior_tests = self.traceability.list_tests(req_id=node_id) if not is_non_leaf else []
        requirement_data = await analyze_and_attach_visual_references(
            workspace_path=self.workspace_path,
            requirements_dir=str(Path(self.requirement_path).expanduser().resolve().parent),
            requirement_data=requirement_data,
            log_cb=self._log,
        )
        requirement_data = self.traceability.get_requirement(node_id) or requirement_data
        visual_reference = requirement_data.get("visual_reference") or []
        self._update_node_session(
            node_id,
            {
                "node_id": node_id,
                "phase_status": {"design": "pending", "test": "pending", "implement": "pending"},
                "requirement_snapshot": {
                    "name": requirement_data.get("name", ""),
                    "description": requirement_data.get("description", ""),
                    "visual_reference": requirement_data.get("visual_reference") or [],
                    "children_ids": requirement_data.get("children_ids") or [],
                    "dependencies": requirement_data.get("dependencies") or [],
                },
                "recent_failure_summary": "",
            },
        )

        if is_non_leaf and not visual_reference:
            self.traceability.clear_node_design_artifacts(node_id)
            context_pipeline.cache.invalidate_file_layers(node_id)
            context_pipeline.cache.invalidate_db_layers(node_id)
            self._update_node_session(
                node_id,
                {
                    "interfaces": [],
                    "materialized_files": [],
                    "test_artifacts": [],
                    "phase_status": {"design": "skipped", "test": "skipped"},
                },
            )
            await self._log(
                "InterfaceDesigner",
                "Skipping non-leaf DESIGN because this node has no visual reference; no UI/API/FUNC/DB interfaces are owned here.",
                status="info",
                node_id=node_id,
            )
            await self._log(
                "TestGenerator",
                "Skipping test generation for non-leaf node.",
                status="info",
                node_id=node_id,
            )
            return True

        await self._log("InterfaceDesigner", "Running interface design.", node_id=node_id)
        interface_result = await self.interface_designer.run(
            node_id=node_id,
            requirement_data=requirement_data,
        )
        interfaces = []
        for item in interface_result.get("interfaces", []):
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized["file_path"] = normalize_workspace_relative_path(normalized.get("file_path"), self.workspace_path)
            interfaces.append(normalized)
        files_written = []
        for path in interface_result.get("files_written") or []:
            normalized_path = normalize_workspace_relative_path(path, self.workspace_path)
            if normalized_path:
                files_written.append(normalized_path)
        if not interfaces:
            await self._log(
                "InterfaceDesigner",
                "Interface design returned no current-node owned interface definitions.",
                status="warning",
                node_id=node_id,
            )

        try:
            prepared_interfaces = self._prepare_interfaces(node_id, interfaces)
        except ValueError as exc:
            await self._log("InterfaceDesigner", str(exc), status="error", node_id=node_id)
            return False
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
        self._update_node_session(
            node_id,
            {
                "interfaces": prepared_interfaces,
                "materialized_files": files_written,
                "phase_status": {"design": "prepared"},
            },
        )
        context_pipeline.cache.invalidate_db_layers(node_id)

        stored_tests: list[dict[str, Any]] = []
        if is_non_leaf:
            self.traceability.clear_node_design_artifacts(node_id)
            self._store_prepared_interfaces(node_id, prepared_interfaces)
            context_pipeline.cache.invalidate_file_layers(node_id)
            context_pipeline.cache.invalidate_db_layers(node_id)
            self._update_node_session(
                node_id,
                {
                    "interfaces": prepared_interfaces,
                    "test_artifacts": [],
                    "phase_status": {"design": "completed", "test": "skipped"},
                },
            )
            await self._log(
                "InterfaceDesigner",
                f"Stored {len(prepared_interfaces)} interface definition(s) into traceability DB.",
                node_id=node_id,
            )
            await self._log(
                "InterfaceDesigner",
                f"Interface artifact summary: {json.dumps(summarize_interface_artifacts(prepared_interfaces), ensure_ascii=False)}",
                node_id=node_id,
            )
            await self._log(
                "TestGenerator",
                "Skipping test generation for non-leaf node; composition nodes only define interfaces.",
                status="info",
                node_id=node_id,
            )
            return True

        await self._log(
            "InterfaceDesigner",
            f"Prepared {len(prepared_interfaces)} interface definition(s) for traceability storage.",
            node_id=node_id,
        )
        await self._log(
            "InterfaceDesigner",
            f"Interface artifact summary: {json.dumps(summarize_interface_artifacts(prepared_interfaces), ensure_ascii=False)}",
            node_id=node_id,
        )

        await self._log("TestGenerator", "Generating tests from agent-selected coverage strategy.", node_id=node_id)
        validation_feedback = ""
        previous_attempt_tests: list[dict[str, Any]] = []
        owned_interface_types = {
            str(interface.get("type", "") or "").strip().upper()
            for interface in prepared_interfaces
        }
        required_test_types: list[str] = []
        if owned_interface_types.intersection({"FUNC", "DB"}):
            required_test_types.append("Unit")
        if "API" in owned_interface_types:
            required_test_types.append("Integration")
        if requirement_data.get("scenarios"):
            required_test_types.append("E2E")
        max_generation_attempts = TEST_GENERATION_ADMISSION_ATTEMPTS
        for generation_attempt in range(1, max_generation_attempts + 1):
            tests, _ = await self.test_generator.run(
                node_id=node_id,
                requirement_data=requirement_data,
                validation_feedback=validation_feedback,
            )
            if tests is None:
                validation_feedback = "DESIGN test generation did not return a valid test manifest."
            else:
                try:
                    candidate_tests = tests
                    retry_preserved_layers: list[str] = []
                    if previous_attempt_tests:
                        candidate_tests, retry_preserved_layers = self._preserve_missing_prior_test_layers(
                            candidate_tests,
                            previous_attempt_tests,
                        )
                    stored_tests = self._prepare_tests(
                        node_id=node_id,
                        tests=candidate_tests,
                        requirement_data=requirement_data,
                        required_test_types=required_test_types,
                    )
                    stored_tests, historical_preserved_layers = self._preserve_missing_prior_test_layers(
                        stored_tests,
                        prior_tests,
                    )
                    preserved_layers = list(dict.fromkeys(
                        [*retry_preserved_layers, *historical_preserved_layers]
                    ))
                    break
                except ValueError as exc:
                    validation_feedback = str(exc)
                    previous_attempt_tests = list(candidate_tests)
            await self._log(
                "TestGenerator",
                f"Generated-test admission rejected attempt {generation_attempt}/{max_generation_attempts}: {validation_feedback}",
                status="error" if generation_attempt == max_generation_attempts else "warning",
                node_id=node_id,
            )
        else:
            return False

        self.traceability.clear_node_design_artifacts(node_id)
        self._store_prepared_interfaces(node_id, prepared_interfaces)
        self._store_prepared_tests(stored_tests)
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
        self._update_node_session(
            node_id,
            {
                "interfaces": prepared_interfaces,
                "test_artifacts": stored_tests,
                "phase_status": {"design": "completed", "test": "completed"},
            },
        )
        await self._log(
            "InterfaceDesigner",
            f"Stored {len(prepared_interfaces)} interface definition(s) into traceability DB.",
            node_id=node_id,
        )
        await self._log(
            "TestGenerator",
            f"Stored {len(stored_tests)} test mapping item(s) into traceability DB.",
            node_id=node_id,
        )
        if preserved_layers:
            await self._log(
                "TestGenerator",
                (
                    "Preserved executable test mappings for omitted layer(s) across DESIGN retry/history: "
                    + ", ".join(preserved_layers)
                    + ". This prevents a DESIGN retry from bypassing regression verification."
                ),
                status="warning",
                node_id=node_id,
            )
        await self._log(
            "TestGenerator",
            f"Test artifact summary: {json.dumps(summarize_test_artifacts(stored_tests), ensure_ascii=False)}",
            node_id=node_id,
        )
        return True

    async def run_implement_phase(self, node_id: str, requirement_data: dict[str, Any]) -> bool:
        is_non_leaf = bool(requirement_data.get("children_ids"))
        if is_non_leaf:
            interfaces = self.traceability.list_interfaces(req_id=node_id)
            self._mark_interfaces_implemented(interfaces)
            self._update_node_session(
                node_id,
                {
                    "phase_status": {"implement": "completed"},
                    "result_state": "CONVERGED",
                },
            )
            await self._log(
                "TestDrivenDeveloper",
                "Non-leaf node completed directly after interface materialization; no TDD batch was scheduled.",
                node_id=node_id,
            )
            return True

        del requirement_data
        self._update_node_session(node_id, {"phase_status": {"implement": "in_progress"}})
        interfaces = self.traceability.list_interfaces(req_id=node_id)
        tests = self.traceability.list_tests(req_id=node_id)
        if not tests:
            await self._log(
                "TestDrivenDeveloper",
                "No node-local tests were registered; skipping TDD implementation for this node.",
                node_id=node_id,
            )
            self._mark_interfaces_implemented(interfaces)
            self._update_node_session(node_id, {"phase_status": {"implement": "completed"}})
            return True

        final_ok = await self._run_tdd_for_node(
            node_id=node_id,
            tests=tests,
        )
        if final_ok:
            self._mark_interfaces_implemented(interfaces)
        self._update_node_session(
            node_id,
            {"phase_status": {"implement": "completed" if final_ok else "failed"}},
        )
        return final_ok

    async def _run_tdd_for_node(
        self,
        *,
        node_id: str,
        tests: list[dict[str, Any]],
        only_test_type: str | None = None,
        run_tests_budget: int = TDD_RUN_TESTS_BUDGET,
        max_agent_sessions: int | None = None,
        protect_test_files: bool = False,
    ) -> bool:
        previous_failure_summary = str(sessions.load_node_session(node_id).get("recent_failure_summary", "") or "")
        groups: dict[str, list[dict[str, Any]]] = {}
        for test in tests:
            test_type = str(test.get("type", "") or "").strip()
            if not test_type:
                continue
            normalized_type = test_type.lower()
            groups.setdefault(normalized_type, []).append(test)

        ordered_types = [test_type for test_type in TDD_BATCH_ORDER if groups.get(test_type.lower())]
        if only_test_type:
            requested_type = canonical_test_type(only_test_type)
            ordered_types = [item for item in ordered_types if item == requested_type]
        if not ordered_types:
            return True

        run_tests_budget = max(1, int(run_tests_budget))

        usage_by_type = {test_type: 0 for test_type in ordered_types}
        result_by_type: dict[str, str] = {}
        agent_handoff_by_type: dict[str, str] = {}
        await self._log(
            "TestDrivenDeveloper",
            "Running leaf TDD sessions in ordered layers with independent budgets: " + " -> ".join(ordered_types) + ".",
            node_id=node_id,
        )
        active_test_type: str | None = None
        recovery_only_type: str | None = None

        async def run_requested_tests(requested_type: str | None = None, requested_files: list[str] | None = None) -> str:
            nonlocal active_test_type
            requested = str(requested_type or "").strip()
            if active_test_type is None:
                return (
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    "No active TDD test layer is currently scheduled.\n"
                )
            if requested.lower() in {"", "all", "current", "next"}:
                selected_type = active_test_type
            else:
                selected_type = canonical_test_type(requested)
                if selected_type is None or selected_type not in ordered_types:
                    return (
                        "Exit Code: 1\n"
                        "STDERR:\n"
                        f"Unsupported current-node test_type={requested!r}. "
                        f"Available ordered layers: {', '.join(ordered_types)}.\n"
                    )
                if selected_type != active_test_type:
                    return (
                        "Exit Code: 1\n"
                        "STDERR:\n"
                        f"The active TDD layer is `{active_test_type}`, but run_tests requested `{selected_type}`. "
                        "The system attempts layers in Unit -> Integration -> E2E order with independent budgets.\n"
                    )

            selected_files = list(dict.fromkeys(
                path
                for value in (requested_files or collect_test_files(groups[selected_type.lower()]))
                if (path := normalize_workspace_relative_path(value, self.workspace_path))
            ))
            registered_files = {
                str(item.get("file_path", "") or "").strip()
                for item in groups[selected_type.lower()]
                if str(item.get("file_path", "") or "").strip()
            }
            unknown = [path for path in selected_files if path not in registered_files]
            if unknown:
                return (
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    f"run_tests({selected_type}) may only execute registered {selected_type} tests for the current node. "
                    f"Unknown files: {', '.join(unknown)}\n"
                )
            used = usage_by_type[selected_type]
            active_run_tests_limit = run_tests_budget + (
                TDD_REGRESSION_RECOVERY_RUNS
                if recovery_only_type == selected_type
                else 0
            )
            if used >= active_run_tests_limit:
                await self._log(
                    "TestDrivenDeveloper",
                    f"`run_tests` {selected_type} budget exhausted at {used}/{active_run_tests_limit}.",
                    status="error",
                    node_id=node_id,
                )
                return (
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    f"run_tests budget exhausted for {selected_type}: {used}/{active_run_tests_limit}.\n"
                )
            usage_by_type[selected_type] = used + 1
            await self._log(
                "TestDrivenDeveloper",
                f"`run_tests` {selected_type} usage {usage_by_type[selected_type]}/{active_run_tests_limit}.",
                node_id=node_id,
            )
            output = await self.app_handler.run_test_group(selected_type, selected_files)
            await self._log(
                "TestDrivenDeveloper",
                (
                    "run_tests raw output\n"
                    f"test_type={selected_type}\n"
                    f"attempt={usage_by_type[selected_type]}/{active_run_tests_limit}\n"
                    f"test_files={json.dumps(selected_files, ensure_ascii=False)}\n"
                    "----- BEGIN RAW TEST OUTPUT -----\n"
                    f"{output.rstrip()}\n"
                    "----- END RAW TEST OUTPUT -----"
                ),
                status="debug",
                node_id=node_id,
            )
            parsed_result = parse_test_results(output)
            exit_code = int(parsed_result.get("exit_code", -1))
            passed = exit_code == 0
            await self._log(
                "TestDrivenDeveloper",
                (
                    f"`run_tests` {selected_type} {'passed' if passed else 'failed'} "
                    f"with Exit Code: {exit_code} "
                    f"on attempt {usage_by_type[selected_type]}/{active_run_tests_limit}: "
                    f"{', '.join(selected_files)}"
                ),
                status="ok" if passed else "error",
                node_id=node_id,
            )
            result_by_type[selected_type] = output
            next_index = ordered_types.index(selected_type) + 1
            next_type = (
                None
                if recovery_only_type
                else ordered_types[next_index] if next_index < len(ordered_types) else None
            )
            exhausted = not passed and usage_by_type[selected_type] >= active_run_tests_limit
            if exhausted and next_type:
                output += (
                    "\n\nARC_TEST_LAYER_BUDGET_EXHAUSTED_CONTINUE:\n"
                    f"- {selected_type} used its final execution ({active_run_tests_limit}/{active_run_tests_limit}) without passing.\n"
                    f"- The system advanced to {next_type}; call run_tests() now and continue in this same agent session.\n"
                    "- Do not edit between layers: use the next layer's first result as fresh evidence.\n"
                    "- Do not return a handoff yet; the final verification sweep will recheck earlier layers after later fixes.\n"
                )
                active_test_type = next_type
            elif exhausted:
                output += (
                    "\n\nARC_TEST_BUDGET_EXHAUSTED:\n"
                    f"- {selected_type} used its final execution ({active_run_tests_limit}/{active_run_tests_limit}).\n"
                    "- This is the final scheduled layer. Do not edit again because no validation execution remains; return a continuation handoff.\n"
                    "ARC_IMPLEMENT_SESSION_COMPLETE\n"
                )
            elif passed and next_type:
                output += (
                    "\n\nARC_TEST_LAYER_STATUS:\n"
                    f"- {selected_type} passed.\n"
                    f"- The system will advance to the next test layer: {next_type}.\n"
                    "- Do not return IMPLEMENTED until all scheduled layers have been attempted and passed.\n"
                )
                active_test_type = next_type
            elif passed:
                all_layers_passed = all(
                    parse_test_results(result_by_type.get(candidate, "")).get("exit_code") == 0
                    for candidate in ordered_types
                    if candidate != selected_type
                )
                output += (
                    "\n\nARC_TEST_LAYER_STATUS:\n"
                    f"- {selected_type} passed.\n"
                    "- This is the last scheduled test layer. You may return IMPLEMENTED only if all earlier scheduled layers also passed.\n"
                    "ARC_IMPLEMENT_SESSION_COMPLETE\n"
                    + ("ARC_ALL_TEST_LAYERS_PASSED\n" if all_layers_passed else "ARC_EARLIER_TEST_LAYER_FAILED\n")
                )
            return output

        output = ""
        session_count = 0
        max_sessions = max(1, int(max_agent_sessions or run_tests_budget * len(ordered_types)))
        for ordered_type in ordered_types:
            active_test_type = ordered_type
            previous_failure_summary = str(sessions.load_node_session(node_id).get("recent_failure_summary", "") or "")
            while parse_test_results(result_by_type.get(ordered_type, "")).get("exit_code") != 0:
                used_before = usage_by_type.get(ordered_type, 0)
                if used_before >= run_tests_budget:
                    break
                if session_count >= max_sessions:
                    await self._log(
                        "TestDrivenDeveloper",
                        f"TDD stopped after {session_count} agent session(s); continuing layer summary with collected results.",
                        status="error",
                        node_id=node_id,
                    )
                    break

                session_count += 1
                if used_before > 0:
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"Resuming TDD agent session {session_count} for `{ordered_type}`; "
                            f"run_tests usage is {used_before}/{run_tests_budget}."
                        ),
                        node_id=node_id,
                    )
                output = await self.test_driven_developer.run(
                    node_id=node_id,
                    test_files=collect_test_files(tests),
                    test_type=ordered_type,
                    node_tests=tests,
                    previous_failure_summary=previous_failure_summary,
                    run_tests_budget=None,
                    run_tests_usage=None,
                    run_tests_executor=run_requested_tests,
                    protect_test_files=protect_test_files,
                    continue_across_layers=len(ordered_types) > 1,
                )
                latest_result = result_by_type.get(ordered_type, "")
                layer_passed = parse_test_results(latest_result).get("exit_code") == 0
                normalized_handoff = output.casefold()
                failed_result_types = [
                    candidate
                    for candidate in ordered_types
                    if candidate in result_by_type
                    and parse_test_results(result_by_type[candidate]).get("exit_code") != 0
                ]
                handoff_type = failed_result_types[-1] if failed_result_types else ""
                if handoff_type and (
                    "next concrete edit target" in normalized_handoff
                    or ("next_file:" in normalized_handoff and "next_action:" in normalized_handoff)
                ):
                    agent_handoff_by_type[handoff_type] = output
                    from agents.context.global_memory import GlobalProjectMemory

                    GlobalProjectMemory(self.workspace_path, self.traceability).record_handoff(
                        requirement=node_id,
                        test_type=handoff_type,
                        test_files=collect_test_files(groups[handoff_type.lower()]),
                        fingerprint=output,
                        protected_files=collect_test_files(tests) if protect_test_files else None,
                    )

                previous_failure_summary = (
                    agent_handoff_by_type.get(ordered_type)
                    or self.test_driven_developer.get_last_verifier_report()
                    or summarize_batch_output(latest_result or output)
                )
                used_after = usage_by_type.get(ordered_type, 0)
                if layer_passed:
                    break
                if used_after >= run_tests_budget:
                    break
                if used_after == used_before:
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"TDD agent session ended without calling run_tests for `{ordered_type}`; "
                            "moving to the next scheduled layer with a fresh budget."
                        ),
                        status="error",
                        node_id=node_id,
                    )
                    break
            active_test_type = None
            if parse_test_results(result_by_type.get(ordered_type, "")).get("exit_code") != 0:
                await self._log(
                    "TestDrivenDeveloper",
                    f"Advancing past `{ordered_type}` without a passing result; the next scheduled layer will start with its own budget.",
                    status="warning",
                    node_id=node_id,
                )

        async def run_zero_token_verification(
            verification_types: list[str],
            *,
            reason: str,
        ) -> list[str]:
            failed: list[str] = []
            for regression_type in verification_types:
                regression_files = collect_test_files(groups[regression_type.lower()])
                await self._log(
                    "TestDrivenDeveloper",
                    f"Final zero-token verification for `{regression_type}` {reason}.",
                    node_id=node_id,
                )
                regression_output = await self.app_handler.run_test_group(
                    regression_type,
                    regression_files,
                )
                regression_passed = parse_test_results(regression_output).get("exit_code") == 0
                await self._log(
                    "TestDrivenDeveloper",
                    f"Final zero-token verification for `{regression_type}` {'passed' if regression_passed else 'failed'}.",
                    status="ok" if regression_passed else "error",
                    node_id=node_id,
                )
                result_by_type[regression_type] = regression_output
                from agents.context.global_memory import GlobalProjectMemory

                GlobalProjectMemory(self.workspace_path, self.traceability).record_test_result(
                    requirement=node_id,
                    test_type=regression_type,
                    test_files=regression_files,
                    output=regression_output,
                )
                if not regression_passed:
                    failed.append(regression_type)
            return failed

        regression_failed_types: list[str] = []
        if len(ordered_types) > 1:
            regression_failed_types = await run_zero_token_verification(
                ordered_types[:-1],
                reason="after later-layer edits",
            )

        for recovery_cycle in range(1, TDD_REGRESSION_RECOVERY_CYCLES + 1):
            if not regression_failed_types:
                break

            for regression_type in regression_failed_types:
                used_before_recovery = usage_by_type.get(regression_type, 0)
                recovery_run_tests_limit = run_tests_budget + TDD_REGRESSION_RECOVERY_RUNS
                if used_before_recovery >= recovery_run_tests_limit:
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"Regression recovery for `{regression_type}` cannot start because its "
                            f"recovery-inclusive run_tests budget is exhausted at "
                            f"{used_before_recovery}/{recovery_run_tests_limit}."
                        ),
                        status="error",
                        node_id=node_id,
                    )
                    continue
                if session_count >= max_sessions:
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"Regression recovery for `{regression_type}` cannot start because the "
                            f"agent-session budget is exhausted at {session_count}/{max_sessions}."
                        ),
                        status="error",
                        node_id=node_id,
                    )
                    continue

                regression_output = result_by_type.get(regression_type, "")
                regression_files = collect_test_files(groups[regression_type.lower()])
                recovery_evidence = (
                    "REGRESSION_REPLAY_FAILURE:\n"
                    f"Final zero-token verification failed for `{regression_type}` after a later layer passed.\n\n"
                    "LATEST_FAILURE_EVIDENCE:\n"
                    f"{regression_output.strip()}\n\n"
                    "RECOVERY_RULE:\n"
                    "Resume from this failing layer in the existing TDD thread. Determine whether the regression "
                    "comes from product code, a generated-test contradiction, or runner configuration; repair the "
                    "correct artifact, then rerun this layer. Do not repeat repository-wide discovery."
                )
                previous_failure_summary = recovery_evidence
                agent_handoff_by_type[regression_type] = recovery_evidence
                recovery_only_type = regression_type
                active_test_type = regression_type

                while parse_test_results(result_by_type.get(regression_type, "")).get("exit_code") != 0:
                    used_before = usage_by_type.get(regression_type, 0)
                    if used_before >= recovery_run_tests_limit or session_count >= max_sessions:
                        break
                    session_count += 1
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"Resuming TDD regression recovery cycle {recovery_cycle} for `{regression_type}` "
                            f"in the existing node thread; run_tests usage is "
                            f"{used_before}/{recovery_run_tests_limit}."
                        ),
                        node_id=node_id,
                    )
                    output = await self.test_driven_developer.run(
                        node_id=node_id,
                        test_files=collect_test_files(tests),
                        test_type=regression_type,
                        node_tests=tests,
                        previous_failure_summary=previous_failure_summary,
                        run_tests_budget=None,
                        run_tests_usage=None,
                        run_tests_executor=run_requested_tests,
                        protect_test_files=protect_test_files,
                        continue_across_layers=False,
                    )
                    latest_result = result_by_type.get(regression_type, "")
                    if parse_test_results(latest_result).get("exit_code") == 0:
                        break
                    used_after = usage_by_type.get(regression_type, 0)
                    previous_failure_summary = (
                        self.test_driven_developer.get_last_verifier_report()
                        or summarize_batch_output(latest_result or output)
                    )
                    if used_after == used_before:
                        await self._log(
                            "TestDrivenDeveloper",
                            (
                                f"Regression recovery session ended without calling run_tests for `{regression_type}`; "
                                "leaving the replay failure unresolved."
                            ),
                            status="error",
                            node_id=node_id,
                        )
                        break

                active_test_type = None
                recovery_only_type = None

            # A regression repair can affect any layer, including the E2E layer that
            # originally passed, so verify the complete node contract before success.
            regression_failed_types = await run_zero_token_verification(
                ordered_types,
                reason=f"after regression recovery cycle {recovery_cycle}",
            )

        final_ok = True
        failure_summaries: list[str] = []
        failed_types: list[str] = []
        for test_type in ordered_types:
            latest_result = result_by_type.get(test_type, "")
            group_passed = parse_test_results(latest_result).get("exit_code") == 0
            used = usage_by_type.get(test_type, 0)
            reported_limit = (
                run_tests_budget + TDD_REGRESSION_RECOVERY_RUNS
                if used > run_tests_budget
                else run_tests_budget
            )
            status_by_test_id = {
                str(test.get("test_id", "")).strip(): group_passed
                for test in groups[test_type.lower()]
                if str(test.get("test_id", "")).strip()
            }
            self.traceability.set_test_pass_statuses(status_by_test_id)
            if group_passed:
                await self._log(
                    "TestDrivenDeveloper",
                    f"TDD batch `{test_type}` passed after {used}/{reported_limit} run_tests call(s).",
                    node_id=node_id,
                )
                continue
            final_ok = False
            failed_types.append(test_type)
            failure_summary = agent_handoff_by_type.get(test_type, "")
            if not failure_summary:
                failure_summary = (
                    summarize_batch_output(latest_result)
                    if latest_result
                    else self.test_driven_developer.get_last_verifier_report()
                    or summarize_batch_output(output)
                )
            failure_summaries.append(f"{test_type}: {failure_summary}")
            detail = "budget exhausted" if used >= reported_limit else "agent session ended before this layer passed"
            await self._log(
                "TestDrivenDeveloper",
                f"TDD batch `{test_type}` did not pass after {used}/{reported_limit} run_tests call(s); {detail}.",
                status="error",
                node_id=node_id,
            )

        context_pipeline.cache.invalidate_db_layers(node_id)
        context_pipeline.cache.invalidate_file_layers(node_id)
        failure_summary = "\n\n".join(failure_summaries)
        self._update_node_session(
            node_id,
            {
                "recent_failure_summary": failure_summary,
                "tdd_handoff": {
                    "last_test_type": failed_types[-1] if failed_types else ordered_types[-1],
                    "last_failed_output_summary": failure_summary,
                    "modified_files": [],
                },
            },
        )
        if not final_ok:
            return False

        unexpected_types = sorted(set(groups) - {item.lower() for item in TDD_BATCH_ORDER})
        if unexpected_types:
            await self._log(
                "TestDrivenDeveloper",
                f"Ignoring unsupported test batch type(s): {', '.join(unexpected_types)}.",
                status="warning",
                node_id=node_id,
            )

        return True

    async def run_checkpoint_micro_run(
        self,
        *,
        node_id: str,
        test_type: str | None,
        run_tests_budget: int,
        max_agent_sessions: int,
        protect_test_files: bool = True,
    ) -> bool:
        """Run one or all existing node test layers under deliberately small budgets."""
        tests = self.traceability.list_tests(req_id=node_id)
        selected = canonical_test_type(test_type) if test_type else None
        if test_type and (
            selected is None
            or not any(
                str(test.get("type", "")).strip().lower() == selected.lower()
                for test in tests
            )
        ):
            raise ValueError(f"No registered {test_type!r} tests found for node {node_id!r}.")
        return await self._run_tdd_for_node(
            node_id=node_id,
            tests=tests,
            only_test_type=selected,
            run_tests_budget=run_tests_budget,
            max_agent_sessions=max_agent_sessions,
            protect_test_files=protect_test_files,
        )

    def _prepare_interfaces(self, node_id: str, interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for interface in interfaces:
            interface_id = str(interface.get("interface_id", "")).strip()
            if not interface_id:
                continue
            existing = self.traceability.get_interface(interface_id)
            if existing:
                try:
                    existing_content = json.loads(str(existing.get("content") or "{}"))
                except json.JSONDecodeError:
                    existing_content = {}
                if isinstance(existing_content, dict):
                    interface = {**existing_content, **interface}
            interface_type = str(interface.get("type") or (existing or {}).get("type") or "").strip().upper()
            if interface_type not in ALLOWED_INTERFACE_TYPES:
                raise ValueError(
                    f"Generated interface `{interface_id}` has invalid `type` {interface.get('type')!r}. "
                    "Interface type must be one of UI, API, FUNC, or DB."
                )
            normalized = {
                **interface,
                "interface_id": interface_id,
                "req_id": node_id,
                "type": interface_type,
                "file_path": (
                    normalize_workspace_relative_path(interface.get("file_path"), self.workspace_path)
                    or ((existing or {}).get("file_path") if existing else "")
                ),
                "first_line": str(interface.get("first_line") or (existing or {}).get("first_line") or "").strip(),
                "callers": normalize_string_list(interface.get("callers")) or normalize_string_list((existing or {}).get("callers")),
                "callees": normalize_string_list(interface.get("callees")) or normalize_string_list((existing or {}).get("callees")),
                "_existing_req_ids": list(existing.get("req_ids", [])) if existing else [],
                "_existing_implemented": bool(existing.get("implemented")) if existing else False,
            }
            prepared.append(normalized)
        return prepared

    def _store_prepared_interfaces(self, node_id: str, interfaces: list[dict[str, Any]]) -> None:
        for interface in interfaces:
            interface_id = str(interface.get("interface_id", "")).strip()
            if not interface_id:
                continue
            req_ids = normalize_string_list(interface.get("_existing_req_ids"))
            if node_id not in req_ids:
                req_ids.append(node_id)
            self.traceability.upsert_interface(
                interface_id=interface_id,
                req_ids=req_ids,
                type=str(interface.get("type", "") or "").strip().upper(),
                content=json.dumps(_strip_internal_fields(interface), ensure_ascii=False),
                file_path=str(interface.get("file_path", "") or "").strip() or None,
                first_line=str(interface.get("first_line", "") or "").strip() or None,
                implemented=bool(interface.get("_existing_implemented")),
                callers=normalize_string_list(interface.get("callers")),
                callees=normalize_string_list(interface.get("callees")),
            )
            self._register_interface_edges(node_id, interface_id, interface)

    def _prepare_tests(
        self,
        *,
        node_id: str,
        tests: list[dict[str, Any]],
        requirement_data: dict[str, Any] | None = None,
        required_test_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        stored: list[dict[str, Any]] = []
        generated_ids: set[str] = set()
        for test in tests:
            if not isinstance(test, dict):
                continue
            raw_test_id = str(test.get("test_id", "")).strip()
            if not raw_test_id:
                continue
            file_path = normalize_workspace_relative_path(test.get("file_path"), self.workspace_path)
            test_type = str(test.get("type", "") or "").strip()
            if not test_type:
                raise ValueError(f"Generated test `{raw_test_id}` is missing `type`.")
            if not file_path:
                raise ValueError(f"Generated test `{raw_test_id}` is missing `file_path`.")
            validation_error = self.app_handler.validate_test_path(test_type, file_path)
            if validation_error:
                raise ValueError(f"Generated test `{raw_test_id}` has an invalid path. {validation_error}")
            content_error = self.app_handler.validate_test_content(test_type, file_path)
            if content_error:
                raise ValueError(f"Generated test `{raw_test_id}` failed test-quality admission. {content_error}")
            if raw_test_id in generated_ids:
                raise ValueError(f"Generated duplicate test id `{raw_test_id}`.")
            generated_ids.add(raw_test_id)
            stored_item = {
                **test,
                "test_id": raw_test_id,
                "req_id": node_id,
                "type": test_type,
                "file_path": file_path,
                "interface_ids": normalize_string_list(test.get("interface_ids")),
                "first_line": str(test.get("first_line", "")).strip(),
            }
            stored.append(stored_item)
        scenarios = [
            str(item.get("scenario_id") or item.get("id") or item.get("name") or "").strip()
            for item in (requirement_data or {}).get("scenarios") or []
            if isinstance(item, dict)
            and str(item.get("scenario_id") or item.get("id") or item.get("name") or "").strip()
        ]
        covered_scenarios = {
            str(item.get("scenario_id") or "").strip()
            for item in stored
            if str(item.get("type") or "").strip().casefold() == "e2e"
            and str(item.get("scenario_id") or "").strip()
        }
        missing_scenarios = [scenario for scenario in scenarios if scenario not in covered_scenarios]
        if missing_scenarios:
            raise ValueError(
                "Generated E2E manifest is missing scenario traceability for: "
                + ", ".join(missing_scenarios)
                + ". Set each E2E item's scenario_id to the exact scenario id/name."
            )
        present_test_types = {
            canonical_test_type(item.get("type"))
            for item in stored
            if canonical_test_type(item.get("type"))
        }
        missing_test_types = [
            test_type
            for test_type in (required_test_types or [])
            if canonical_test_type(test_type) not in present_test_types
        ]
        if missing_test_types:
            raise ValueError(
                "Generated test manifest is missing required owned-interface layer(s): "
                + ", ".join(missing_test_types)
                + ". FUNC/DB ownership requires Unit, API ownership requires Integration, "
                "and declared scenarios require E2E."
            )
        return stored

    def _preserve_missing_prior_test_layers(
        self,
        generated_tests: list[dict[str, Any]],
        prior_tests: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Keep still-executable layers from disappearing during a DESIGN retry."""
        merged = list(generated_tests)
        generated_layers = {
            canonical_test_type(item.get("type"))
            for item in generated_tests
            if canonical_test_type(item.get("type"))
        }
        generated_ids = {
            str(item.get("test_id", "") or "").strip()
            for item in generated_tests
        }
        preserved_layers: list[str] = []
        for test_type in TDD_BATCH_ORDER:
            if test_type in generated_layers:
                continue
            preserved = []
            for prior in prior_tests:
                if canonical_test_type(prior.get("type")) != test_type:
                    continue
                test_id = str(prior.get("test_id", "") or "").strip()
                file_path = normalize_workspace_relative_path(prior.get("file_path"), self.workspace_path)
                if not test_id or test_id in generated_ids or not file_path:
                    continue
                if not (Path(self.workspace_path) / file_path).is_file():
                    continue
                preserved.append(
                    {
                        **prior,
                        "test_id": test_id,
                        "file_path": file_path,
                        "type": test_type,
                        "passed": None,
                        "_preserved_from_previous_manifest": True,
                    }
                )
                generated_ids.add(test_id)
            if preserved:
                merged.extend(preserved)
                preserved_layers.append(test_type)
        return merged, preserved_layers

    def _store_prepared_tests(self, tests: list[dict[str, Any]]) -> None:
        for test in tests:
            self.traceability.upsert_test(
                test_id=str(test.get("test_id", "") or "").strip(),
                req_id=str(test.get("req_id", "") or "").strip(),
                interface_ids=normalize_string_list(test.get("interface_ids")),
                type=str(test.get("type", "") or "").strip(),
                file_path=str(test.get("file_path", "") or "").strip() or None,
                first_line=str(test.get("first_line", "") or "").strip() or None,
                passed=None,
                scenario_id=str(test.get("scenario_id", "") or "").strip() or None,
            )

    def _register_interface_edges(self, node_id: str, interface_id: str, interface: dict[str, Any]) -> None:
        for caller_id in normalize_string_list(interface.get("callers")):
            caller = self.traceability.get_interface(caller_id)
            if not caller:
                continue
            for source_req_id in caller.get("req_ids", []):
                if source_req_id and source_req_id != node_id:
                    self.traceability.insert_call_edge(
                        source_req_id=source_req_id,
                        target_req_id=node_id,
                        from_interface_id=caller_id,
                        to_interface_id=interface_id,
                        edge_type="cross_req",
                    )
        for callee_id in normalize_string_list(interface.get("callees")):
            callee = self.traceability.get_interface(callee_id)
            if not callee:
                continue
            for target_req_id in callee.get("req_ids", []):
                if target_req_id and target_req_id != node_id:
                    self.traceability.insert_call_edge(
                        source_req_id=node_id,
                        target_req_id=target_req_id,
                        from_interface_id=interface_id,
                        to_interface_id=callee_id,
                        edge_type="cross_req",
                    )

    def _mark_interfaces_implemented(self, interfaces: list[dict[str, Any]]) -> None:
        for interface in interfaces:
            interface_id = str(interface.get("interface_id", "") or "").strip()
            if interface_id:
                self.traceability.set_interface_implemented(interface_id, True)

    def _update_node_session(self, node_id: str, patch: dict[str, Any]) -> None:
        sessions.merge_node_session(node_id, patch)
        context_pipeline.cache.invalidate_db_layers(node_id)

    async def _log(
        self,
        agent_name: str,
        message: str,
        status: str | None = None,
        node_id: str | None = None,
    ) -> None:
        if self.log_cb is None:
            return
        result = self.log_cb(agent_name, message, status, node_id)
        if hasattr(result, "__await__"):
            await result


def collect_test_files(tests: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for test in tests:
        file_path = str(test.get("file_path", "")).strip()
        if file_path and file_path not in seen:
            seen.append(file_path)
    return seen


def canonical_test_type(value: str) -> str | None:
    normalized = str(value or "").strip().lower()
    for test_type in TDD_BATCH_ORDER:
        if normalized == test_type.lower():
            return test_type
    return None


def summarize_interface_artifacts(interfaces: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(interfaces),
        "items": [
            {
                "id": str(item.get("interface_id", "") or "").strip(),
                "type": str(item.get("type", "") or "").strip(),
                "path": str(item.get("file_path", "") or "").strip(),
                "responsibility": str(item.get("responsibility", "") or item.get("name", "") or "").strip(),
            }
            for item in interfaces
            if isinstance(item, dict)
        ],
    }


def summarize_test_artifacts(tests: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(tests),
        "items": [
            {
                "id": str(item.get("test_id", "") or "").strip(),
                "type": str(item.get("type", "") or "").strip(),
                "path": str(item.get("file_path", "") or "").strip(),
                "interfaces": normalize_string_list(item.get("interface_ids")),
            }
            for item in tests
            if isinstance(item, dict)
        ],
    }


def normalize_workspace_relative_path(value: Any, workspace_path: str) -> str:
    path = normalize_windows_extended_prefix_text(value)
    if not path:
        return ""
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path == "/workspace":
        return ""
    if path.startswith("/workspace/"):
        return path[len("/workspace/") :].lstrip("/")

    workspace = normalize_windows_extended_prefix_text(Path(workspace_path).expanduser().resolve()).rstrip("/")
    if path == workspace:
        return ""
    if path.startswith(workspace + "/"):
        return path[len(workspace) + 1 :].lstrip("/")
    return path.lstrip("/")


def summarize_batch_output(batch_output: str, max_lines: int = 30) -> str:
    lines = [line for line in (batch_output or "").splitlines() if line.strip()]
    if len(lines) > max_lines:
        lines = ["...[truncated]", *lines[-max_lines:]]
    return "\n".join(lines)


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _strip_internal_fields(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if not str(key).startswith("_")}
