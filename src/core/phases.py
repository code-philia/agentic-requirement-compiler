from __future__ import annotations

import json
import hashlib
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
TDD_RUN_TESTS_BUDGET = 5
ALLOWED_INTERFACE_TYPES = {"API", "FUNC", "DB"}
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
                    "file_groups": {"frontend": [], "API": [], "FUNC": [], "DB": [], "shared": []},
                    "materialized_files": [],
                    "test_artifacts": [],
                    "phase_status": {"design": "skipped", "test": "skipped"},
                },
            )
            await self._log(
                "InterfaceDesigner",
                "Skipping parent layout work because this node has no visual reference.",
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
        try:
            file_groups, prepared_interfaces = self._prepare_design_files(node_id, interface_result.get("files"), is_non_leaf)
        except ValueError as exc:
            await self._log("InterfaceDesigner", str(exc), status="error", node_id=node_id)
            return False
        files_written = [path for paths in file_groups.values() for path in paths]
        # A retry may reuse working UI without rewriting it; keep its code locations.
        previous_files = sessions.load_node_session(node_id).get("materialized_files") or []
        files_written = list(dict.fromkeys([*previous_files, *files_written]))
        if not prepared_interfaces and not files_written and not is_non_leaf:
            await self._log(
                "InterfaceDesigner",
                "Node design returned no code files.",
                status="warning",
                node_id=node_id,
            )

        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
        self._update_node_session(
            node_id,
            {
                "interfaces": prepared_interfaces,
                "file_groups": file_groups,
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
                "Skipping test generation for parent layout work; no UI interfaces are modeled.",
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
        tests, _ = await self.test_generator.run(
            node_id=node_id,
            requirement_data=requirement_data,
        )
        if tests is None:
            await self._log(
                "TestGenerator",
                "DESIGN test generation did not return a valid test manifest.",
                status="error",
                node_id=node_id,
            )
            return False

        try:
            stored_tests = self._prepare_tests(node_id=node_id, tests=tests)
        except ValueError as exc:
            await self._log("TestGenerator", str(exc), status="error", node_id=node_id)
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
        await self._log(
            "TestGenerator",
            f"Test artifact summary: {json.dumps(summarize_test_artifacts(stored_tests), ensure_ascii=False)}",
            node_id=node_id,
        )
        return True

    async def run_test_generation_phase(
        self,
        node_id: str,
        requirement_data: dict[str, Any],
        *,
        intent: str,
        replace_test_id: str | None = None,
    ) -> bool:
        normalized_intent = str(intent or "").strip()
        if not normalized_intent:
            await self._log("TestGenerator", "A test intent is required.", status="error", node_id=node_id)
            return False
        if requirement_data.get("children_ids"):
            await self._log(
                "TestGenerator",
                "Intent-based test generation is only available for leaf requirement nodes.",
                status="error",
                node_id=node_id,
            )
            return False

        normalized_replace_test_id = str(replace_test_id or "").strip()
        existing_tests = self.traceability.list_tests(req_id=node_id)
        existing_by_id = {
            str(test.get("test_id", "") or "").strip(): test
            for test in existing_tests
            if str(test.get("test_id", "") or "").strip()
        }
        existing_test = existing_by_id.get(normalized_replace_test_id)
        if normalized_replace_test_id and existing_test is None:
            await self._log(
                "TestGenerator",
                f"Test regeneration received unregistered test id: {normalized_replace_test_id}",
                status="error",
                node_id=node_id,
            )
            return False
        action = f"Regenerating test {normalized_replace_test_id}" if normalized_replace_test_id else "Adding tests"
        await self._log("TestGenerator", f"{action} for intent: {normalized_intent}", node_id=node_id)
        tests, _ = await self.test_generator.run(
            node_id=node_id,
            requirement_data=requirement_data,
            test_intent=normalized_intent,
            replace_test_id=normalized_replace_test_id or None,
        )
        if tests is None:
            await self._log("TestGenerator", "Test generation did not return a valid manifest.", status="error", node_id=node_id)
            return False
        try:
            prepared_tests = self._prepare_tests(node_id=node_id, tests=tests)
        except ValueError as exc:
            await self._log("TestGenerator", str(exc), status="error", node_id=node_id)
            return False

        existing_ids = set(existing_by_id)
        if normalized_replace_test_id:
            generated_ids = {str(test.get("test_id", "") or "").strip() for test in prepared_tests}
            if generated_ids != {normalized_replace_test_id}:
                await self._log(
                    "TestGenerator",
                    f"Test regeneration must return only the selected test id: {normalized_replace_test_id}",
                    status="error",
                    node_id=node_id,
                )
                return False
            original_path = str(existing_test.get("file_path", "") or "").strip()
            generated_path = str(prepared_tests[0].get("file_path", "") or "").strip() if prepared_tests else ""
            if original_path != generated_path:
                await self._log(
                    "TestGenerator",
                    f"Test regeneration must preserve the selected test file path: {original_path}",
                    status="error",
                    node_id=node_id,
                )
                return False
        duplicate_ids = sorted(
            test_id
            for test_id in (str(test.get("test_id", "") or "").strip() for test in prepared_tests)
            if test_id in existing_ids and test_id != normalized_replace_test_id
        )
        if duplicate_ids:
            await self._log(
                "TestGenerator",
                f"Test generation would overwrite existing test id(s): {', '.join(duplicate_ids)}",
                status="error",
                node_id=node_id,
            )
            return False

        self._store_prepared_tests(prepared_tests)
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
        await self._log(
            "TestGenerator",
            f"Stored {len(prepared_tests)} test mapping item(s) for the requested generation.",
            node_id=node_id,
        )
        return True

    async def run_implement_phase(
        self,
        node_id: str,
        requirement_data: dict[str, Any],
        test_ids: list[str] | None = None,
    ) -> bool:
        is_non_leaf = bool(requirement_data.get("children_ids"))
        selected_test_ids = list(dict.fromkeys(str(test_id or "").strip() for test_id in test_ids or [] if str(test_id or "").strip()))
        if selected_test_ids and is_non_leaf:
            await self._log(
                "TestDrivenDeveloper",
                "Selected-test TDD is only available for leaf requirement nodes.",
                status="error",
                node_id=node_id,
            )
            return False
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
                "Parent layout work completed directly after DESIGN; no TDD batch was scheduled.",
                node_id=node_id,
            )
            return True

        del requirement_data
        if not selected_test_ids:
            self._update_node_session(node_id, {"phase_status": {"implement": "in_progress"}})
        interfaces = self.traceability.list_interfaces(req_id=node_id)
        tests = self.traceability.list_tests(req_id=node_id)
        if selected_test_ids:
            tests_by_id = {
                str(test.get("test_id", "") or "").strip(): test
                for test in tests
                if str(test.get("test_id", "") or "").strip()
            }
            unknown_test_ids = [test_id for test_id in selected_test_ids if test_id not in tests_by_id]
            if unknown_test_ids:
                await self._log(
                    "TestDrivenDeveloper",
                    f"Selected-test TDD received unregistered test id(s): {', '.join(unknown_test_ids)}",
                    status="error",
                    node_id=node_id,
                )
                return False
            tests = [tests_by_id[test_id] for test_id in selected_test_ids]
        if not tests:
            await self._log(
                "TestDrivenDeveloper",
                "No node-local tests were registered; skipping TDD implementation for this node.",
                node_id=node_id,
            )
            if not selected_test_ids:
                self._mark_interfaces_implemented(interfaces)
                self._update_node_session(node_id, {"phase_status": {"implement": "completed"}})
            return True

        final_ok = await self._run_tdd_for_node(
            node_id=node_id,
            tests=tests,
        )
        if final_ok and not selected_test_ids:
            self._mark_interfaces_implemented(interfaces)
        if not selected_test_ids:
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
        if not ordered_types:
            return True

        usage_by_type = {test_type: 0 for test_type in ordered_types}
        result_by_type: dict[str, str] = {}
        await self._log(
            "TestDrivenDeveloper",
            "Running leaf TDD sessions in ordered layers with independent budgets: " + " -> ".join(ordered_types) + ".",
            node_id=node_id,
        )
        active_test_type: str | None = None

        async def run_requested_tests(requested_type: str | None = None, requested_files: list[str] | None = None) -> str:
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

            selected_files = [
                path
                for value in (requested_files or collect_test_files(groups[selected_type.lower()]))
                if (path := normalize_workspace_relative_path(value, self.workspace_path))
            ]
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
            if used >= TDD_RUN_TESTS_BUDGET:
                await self._log(
                    "TestDrivenDeveloper",
                    f"`run_tests` {selected_type} budget exhausted at {used}/{TDD_RUN_TESTS_BUDGET}.",
                    status="error",
                    node_id=node_id,
                )
                return (
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    f"run_tests budget exhausted for {selected_type}: {used}/{TDD_RUN_TESTS_BUDGET}.\n"
                )
            usage_by_type[selected_type] = used + 1
            await self._log(
                "TestDrivenDeveloper",
                f"`run_tests` {selected_type} usage {usage_by_type[selected_type]}/{TDD_RUN_TESTS_BUDGET}.",
                node_id=node_id,
            )
            output = await self.app_handler.run_test_group(selected_type, selected_files)
            await self._log(
                "TestDrivenDeveloper",
                (
                    "run_tests raw output\n"
                    f"test_type={selected_type}\n"
                    f"attempt={usage_by_type[selected_type]}/{TDD_RUN_TESTS_BUDGET}\n"
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
                    f"on attempt {usage_by_type[selected_type]}/{TDD_RUN_TESTS_BUDGET}: "
                    f"{', '.join(selected_files)}"
                ),
                status="ok" if passed else "error",
                node_id=node_id,
            )
            result_by_type[selected_type] = output
            next_index = ordered_types.index(selected_type) + 1
            next_type = ordered_types[next_index] if next_index < len(ordered_types) else None
            if passed and next_type:
                output += (
                    "\n\nARC_TEST_LAYER_STATUS:\n"
                    f"- {selected_type} passed.\n"
                    f"- The system will advance to the next test layer: {next_type}.\n"
                    "- Do not return IMPLEMENTED until all scheduled layers have been attempted and passed.\n"
                )
            elif passed:
                output += (
                    "\n\nARC_TEST_LAYER_STATUS:\n"
                    f"- {selected_type} passed.\n"
                    "- This is the last scheduled test layer. You may return IMPLEMENTED only if all earlier scheduled layers also passed.\n"
                )
            return output

        output = ""
        session_count = 0
        max_sessions = max(1, TDD_RUN_TESTS_BUDGET * len(ordered_types))
        for ordered_type in ordered_types:
            active_test_type = ordered_type
            previous_failure_summary = str(sessions.load_node_session(node_id).get("recent_failure_summary", "") or "")
            while parse_test_results(result_by_type.get(ordered_type, "")).get("exit_code") != 0:
                used_before = usage_by_type.get(ordered_type, 0)
                if used_before >= TDD_RUN_TESTS_BUDGET:
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
                            f"run_tests usage is {used_before}/{TDD_RUN_TESTS_BUDGET}."
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
                )

                latest_result = result_by_type.get(ordered_type, "")
                previous_failure_summary = (
                    self.test_driven_developer.get_last_verifier_report()
                    or summarize_batch_output(latest_result or output)
                )
                used_after = usage_by_type.get(ordered_type, 0)
                if parse_test_results(latest_result).get("exit_code") == 0:
                    break
                if used_after >= TDD_RUN_TESTS_BUDGET:
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

        final_ok = True
        failure_summaries: list[str] = []
        failed_types: list[str] = []
        for test_type in ordered_types:
            latest_result = result_by_type.get(test_type, "")
            group_passed = parse_test_results(latest_result).get("exit_code") == 0
            status_by_test_id = {
                str(test.get("test_id", "")).strip(): group_passed
                for test in groups[test_type.lower()]
                if str(test.get("test_id", "")).strip()
            }
            self.traceability.set_test_pass_statuses(status_by_test_id)
            if group_passed:
                await self._log(
                    "TestDrivenDeveloper",
                    f"TDD batch `{test_type}` passed after {usage_by_type.get(test_type, 0)}/{TDD_RUN_TESTS_BUDGET} run_tests call(s).",
                    node_id=node_id,
                )
                continue
            final_ok = False
            failed_types.append(test_type)
            failure_summary = (
                summarize_batch_output(latest_result)
                if latest_result
                else self.test_driven_developer.get_last_verifier_report()
                or summarize_batch_output(output)
            )
            failure_summaries.append(f"{test_type}: {failure_summary}")
            used = usage_by_type.get(test_type, 0)
            detail = "budget exhausted" if used >= TDD_RUN_TESTS_BUDGET else "agent session ended before this layer passed"
            await self._log(
                "TestDrivenDeveloper",
                f"TDD batch `{test_type}` did not pass after {used}/{TDD_RUN_TESTS_BUDGET} run_tests call(s); {detail}.",
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

    def _prepare_design_files(self, node_id: str, groups: Any, is_non_leaf: bool) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
        if not isinstance(groups, dict):
            raise ValueError("DESIGN must return files grouped as frontend/API/FUNC/DB/shared.")
        if set(groups) - {"frontend", "API", "FUNC", "DB", "shared"}:
            raise ValueError("Unknown DESIGN file group.")
        root = Path(self.workspace_path).resolve()
        normalized: dict[str, list[str]] = {}
        prepared: list[dict[str, Any]] = []
        owners = self.traceability.list_interfaces()
        for layer in ("frontend", "API", "FUNC", "DB", "shared"):
            paths = groups.get(layer, [])
            if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                raise ValueError(f"DESIGN files.{layer} must be a list of paths.")
            normalized[layer] = []
            for value in paths:
                path = normalize_workspace_relative_path(value, self.workspace_path)
                target = root / path
                if not path or not target.resolve().is_relative_to(root) or not target.is_file():
                    raise ValueError(f"DESIGN returned a missing or unsafe file: {value}")
                if path.split("/")[0] in {".arc", ".git", "requirements"}:
                    raise ValueError(f"Compiler control files cannot be DESIGN artifacts: {path}")
                path = target.resolve().relative_to(root).as_posix()
                if path.split("/")[0] in {".arc", ".git", "requirements"}:
                    raise ValueError(f"Compiler control files cannot be DESIGN artifacts: {path}")
                if path in normalized[layer]:
                    continue
                normalized[layer].append(path)
                if layer not in ALLOWED_INTERFACE_TYPES:
                    continue
                if is_non_leaf:
                    raise ValueError("Parent layout DESIGN cannot own backend files.")
                if self.app_type == "web" and not path.startswith("backend/"):
                    raise ValueError(f"Backend file groups require backend paths: {path}")
                for record in owners:
                    if record.get("file_path") == path and (
                        str(record.get("interface_id", "")).startswith("GLOBAL:DB:")
                        or node_id not in record.get("req_ids", [])
                    ):
                        raise ValueError(f"Backend file belongs to another requirement or global database: {path}; list shared infrastructure under shared.")
                digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
                record_id = f"{node_id}:FILE:{layer}:{digest}"
                existing = self.traceability.get_interface(record_id) or {}
                prepared.append({
                    "interface_id": record_id, "req_id": node_id,
                    "type": layer, "file_path": path,
                    "_existing_implemented": bool(existing.get("implemented")),
                })
        return normalized, prepared

    def _store_prepared_interfaces(self, node_id: str, interfaces: list[dict[str, Any]]) -> None:
        # Retain the existing traceability table/API for consumers; its rows are now
        # file mappings, not duplicated source contracts or inferred call graphs.
        for item in interfaces:
            self.traceability.upsert_interface(
                interface_id=item["interface_id"], req_ids=[node_id], type=item["type"],
                content=json.dumps({"kind": "code_file", "file_path": item["file_path"]}),
                file_path=item["file_path"], implemented=bool(item.get("_existing_implemented")),
            )

    def _prepare_tests(
        self,
        *,
        node_id: str,
        tests: list[dict[str, Any]],
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
            if raw_test_id in generated_ids:
                raise ValueError(f"Generated duplicate test id `{raw_test_id}`.")
            generated_ids.add(raw_test_id)
            stored_item = {
                **test,
                "test_id": raw_test_id,
                "req_id": node_id,
                "type": test_type,
                "file_path": file_path,
                "interface_ids": [],
                "first_line": "",
            }
            stored.append(stored_item)
        return stored

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
