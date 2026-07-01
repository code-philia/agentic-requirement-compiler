import json
import os
import re
from typing import Any, Awaitable, Callable

import utils
from agents.context_pipeline import context_pipeline
from agents.tools.cli_tools import parse_test_results
from app_types import create_app_type_handler
from traceability.database import (
    clear_node_design_artifacts,
    get_requirement_by_id,
    get_interface_by_id,
    get_interfaces_by_req_id,
    get_node_contract,
    get_node_state,
    get_tests_by_req_id,
    insert_call_edge,
    insert_interface,
    insert_test,
    upsert_node_contract,
    update_interface_implemented,
    update_interface_implemented_status,
    update_test_pass_statuses,
    update_interface_req_ids,
)
from utils import extract_json_array_from_markdown, extract_modified_files_from_messages

TEST_TYPE_ORDER = ["Unit", "Integration", "E2E"]
DEFAULT_TDD_TEST_BUDGET = 10
TEST_LEVEL_TO_TYPES = {
    "light": ["Unit"],
    "middle": ["Unit", "Integration"],
    "heavy": ["Unit", "Integration", "E2E"],
}


class WorkflowPhaseRunner:
    """Run the heavy DESIGN and IMPLEMENT phases while the workflow manager stays lightweight."""

    def __init__(
        self,
        workspace_path: str,
        requirement_path: str,
        app_type: str,
        test_level: str,
        interface_designer,
        test_generator,
        test_driven_developer,
        log_cb: Callable[[str, str, str | None, str | None], Awaitable[None] | None],
    ):
        self.workspace_path = workspace_path
        self.requirement_path = requirement_path
        self.app_type = app_type
        normalized_test_level = str(test_level or "middle").strip().lower()
        self.test_level = normalized_test_level if normalized_test_level in TEST_LEVEL_TO_TYPES else "middle"
        self.interface_designer = interface_designer
        self.test_generator = test_generator
        self.test_driven_developer = test_driven_developer
        self.log_cb = log_cb
        self.app_handler = create_app_type_handler(
            app_type=app_type,
            workspace_path=workspace_path,
            requirement_path=requirement_path,
            interface_designer=interface_designer,
            log_cb=log_cb,
        )

    def _update_node_session(self, node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        session = utils.merge_node_session(node_id, patch)
        context_pipeline.cache.invalidate(node_id, "node_session")
        return session

    @staticmethod
    def _has_visual_reference_hint(requirement_data: dict[str, Any]) -> bool:
        visual_reference = requirement_data.get("visual_reference") or []
        if visual_reference:
            return True
        description = str(requirement_data.get("description", "") or "")
        return bool(re.search(r"!\[[^\]]*\]\(([^)]+)\)", description))

    def _classify_non_leaf_work(self, node_id: str, requirement_data: dict[str, Any]) -> str:
        scenarios = requirement_data.get("scenarios") or []
        if scenarios:
            return "non_leaf_full"
        if self._has_visual_reference_hint(requirement_data):
            return "non_leaf_ui_only"
        return "skip"

    def _build_base_node_session(
        self,
        node_id: str,
        requirement_data: dict[str, Any],
        node_role: str,
        design_mode: str,
    ) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "node_role": node_role,
            "design_mode": design_mode,
            "phase_status": {
                "understand": "pending",
                "design": "pending",
                "spec": "pending",
                "test": "pending",
                "implement": "pending",
            },
            "requirement_snapshot": {
                "name": requirement_data.get("name", ""),
                "description": requirement_data.get("description", ""),
                "children_ids": requirement_data.get("children_ids") or [],
                "dependencies": requirement_data.get("dependencies") or [],
            },
            "test_level": self.test_level,
        }

    def _build_test_plan(self, tests: list[dict[str, Any]]) -> dict[str, Any]:
        grouped = {
            "unit_files": [],
            "integration_files": [],
            "e2e_files": [],
            "test_ids": [],
        }
        for test in tests:
            if not isinstance(test, dict):
                continue
            test_type = str(test.get("type", "")).strip()
            file_path = str(test.get("file_path", "")).strip()
            test_id = str(test.get("test_id", "")).strip()
            if test_id:
                grouped["test_ids"].append(test_id)
            if test_type == "Unit" and file_path and file_path not in grouped["unit_files"]:
                grouped["unit_files"].append(file_path)
            elif test_type == "Integration" and file_path and file_path not in grouped["integration_files"]:
                grouped["integration_files"].append(file_path)
            elif test_type == "E2E" and file_path and file_path not in grouped["e2e_files"]:
                grouped["e2e_files"].append(file_path)
        return grouped

    def _summarize_batch_output(self, batch_output: str, max_lines: int = 30) -> str:
        lines = [line for line in (batch_output or "").splitlines() if line.strip()]
        if not lines:
            return ""
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
            lines.insert(0, "...[truncated]")
        return "\n".join(lines)

    def _build_empty_test_plan(self) -> dict[str, Any]:
        return {
            "unit_files": [],
            "integration_files": [],
            "e2e_files": [],
            "test_ids": [],
        }

    def _get_selected_test_types(self) -> list[str]:
        return list(TEST_LEVEL_TO_TYPES.get(self.test_level, TEST_LEVEL_TO_TYPES["middle"]))

    def _get_test_generation_mode(self) -> str:
        selected = self._get_selected_test_types()
        if selected == ["Unit"]:
            return "Unit"
        if selected == ["Unit", "Integration"]:
            return "Unit+Integration"
        return "All"

    def _determine_non_leaf_result_state(self, requirement_data: dict[str, Any]) -> str:
        child_ids = [
            str(child_id).strip()
            for child_id in (requirement_data.get("children_ids") or [])
            if str(child_id).strip()
        ]
        for child_id in child_ids:
            child_state = get_node_state(child_id) or {}
            if str(child_state.get("state", "")).strip().upper() == "FAILED":
                return "CONVERGED_WITH_FAILED_CHILDREN"
        return "CONVERGED"

    async def _run_non_leaf_shell_test_verification(
        self,
        node_id: str,
        tests: list[dict[str, Any]],
    ) -> tuple[bool, str, dict[str, bool | None]]:
        aggregated_outputs: list[str] = []
        merged_statuses: dict[str, bool | None] = {}
        all_passed = True

        for test_type in TEST_TYPE_ORDER:
            typed_tests = [test for test in tests if str(test.get("type", "")).strip() == test_type]
            if not typed_tests:
                continue
            test_files = self._collect_test_files(typed_tests)
            batch_output = await self._run_test_batch_for_agent(node_id=node_id, test_type=test_type, test_files=test_files)
            batch_passed, batch_statuses = self._map_statuses_from_batch_output(
                tests=typed_tests,
                batch_output=batch_output,
            )
            merged_statuses.update(batch_statuses)
            aggregated_outputs.append(f"## {test_type}\n{batch_output}")
            if not batch_passed:
                all_passed = False

        return all_passed, "\n\n".join(aggregated_outputs), merged_statuses

    async def run_design_phase(self, node_id: str, requirement_data: dict[str, Any]) -> bool:
        is_leaf = not bool(requirement_data.get("children_ids"))
        design_mode = "leaf_full" if is_leaf else self._classify_non_leaf_work(node_id, requirement_data)
        self._update_node_session(
            node_id,
            self._build_base_node_session(
                node_id=node_id,
                requirement_data=requirement_data,
                node_role="leaf" if is_leaf else "non_leaf",
                design_mode=design_mode,
            ),
        )

        if not is_leaf and design_mode == "skip":
            self._update_node_session(
                node_id,
                {
                    "execution_mode": "skipped_non_leaf",
                    "reason": "no visual reference and no scenarios",
                    "phase_status": {
                        "design": "completed",
                        "spec": "skipped",
                        "test": "skipped",
                        "implement": "skipped",
                    },
                },
            )
            await self.log_cb(
                "InterfaceDesigner",
                "Skipping DESIGN for non-leaf node because it has neither visual reference nor scenarios.",
                None,
                node_id,
            )
            return True

        await self.interface_designer.parse_and_store_visual_elements(
            self.workspace_path,
            os.path.dirname(os.path.abspath(self.requirement_path)),
            requirement_data,
        )
        requirement_data = get_requirement_by_id(node_id) or requirement_data

        await self.log_cb(
            "InterfaceDesigner",
            f"Running unified design session for `{design_mode}`: understand, design, specify, and materialize interfaces...",
            None,
            node_id,
        )
        design_bundle, design_messages = await self.interface_designer.design_bundle(
            node_id=node_id,
            requirement_data=requirement_data,
            design_mode=design_mode,
        )
        interfaces = design_bundle.get("interfaces", [])
        interface_spec = design_bundle.get("interface_spec", [])

        if not interfaces:
            await self.log_cb(
                "InterfaceDesigner",
                "Unified design session did not return a valid interface JSON array.",
                "error",
                node_id,
            )
            return False
        if not interface_spec:
            await self.log_cb(
                "InterfaceDesigner",
                "Unified design session did not return a valid interface spec array.",
                "error",
                node_id,
            )
            return False

        self._update_node_session(node_id, {"phase_status": {"understand": "completed"}})
        await self.log_cb(
            "InterfaceDesigner",
            "Completed unified design session and persisted node session context.",
            None,
            node_id,
        )

        clear_node_design_artifacts(node_id)
        self._store_interfaces(node_id, interfaces)
        upsert_node_contract(node_id, self._build_frozen_node_contract(node_id, requirement_data, interfaces, []))
        context_pipeline.cache.invalidate_db_layers(node_id)
        self._update_node_session(
            node_id,
            {
                "interface_ir": interfaces,
                "interfaces": interfaces,
                "interface_spec": interface_spec,
                "phase_status": {
                    "design": "completed",
                    "spec": "completed",
                },
            },
        )
        await self.log_cb(
            "InterfaceDesigner",
            f"Stored {len(interfaces)} interface definition(s) into traceability DB.",
            None,
            node_id,
        )

        for interface in interfaces:
            if not isinstance(interface, dict):
                continue
            interface_id = str(interface.get("interface_id", "")).strip()
            if interface_id:
                update_interface_implemented(interface_id, True)

        materialized = interfaces
        materialized_files = extract_modified_files_from_messages(design_messages)
        if materialized_files:
            context_pipeline.cache.invalidate_file_layers(node_id)
            await self.log_cb(
                "InterfaceDesigner",
                f"Materialized files: {', '.join(sorted(materialized_files))}",
                None,
                node_id,
            )

        self._update_node_session(
            node_id,
            {
                "materialized_interfaces": materialized,
                "interfaces": materialized,
                "materialized_files": materialized_files,
            },
        )
        await self.log_cb(
            "InterfaceDesigner",
            f"Materialized {len(interfaces)} interface(s) into code during the unified design session.",
            None,
            node_id,
        )

        if not is_leaf and design_mode == "non_leaf_ui_only":
            self._update_node_session(
                node_id,
                {
                    "test_plan": self._build_empty_test_plan(),
                    "test_artifacts": [],
                    "phase_status": {"test": "skipped"},
                },
            )
            await self.log_cb(
                "TestGenerator",
                "Skipping test generation for non-leaf UI-only node.",
                None,
                node_id,
            )
            return True

        await self.log_cb(
            "TestGenerator",
            "Generating tests from the designed interfaces...",
            None,
            node_id,
        )
        messages, tools = self.test_generator.build_initial_messages(
            node_id=node_id,
            requirement_data=requirement_data,
            interfaces_ir=interfaces,
            test_type=self._get_test_generation_mode(),
            is_leaf=is_leaf,
            node_understanding=node_understanding,
            interface_spec=interface_spec,
            design_mode=design_mode,
        )
        test_output, test_messages = await self.test_generator.run_from_messages(
            messages=messages,
            node_id=node_id,
            max_steps=30,
            tools=tools,
        )
        tests = extract_json_array_from_markdown(test_output)
        if not tests:
            await self.log_cb(
                "TestGenerator",
                "DESIGN phase test generation did not return a valid test JSON array.",
                "error",
                node_id,
            )
            return False

        try:
            self._store_tests(
                node_id,
                tests,
                is_leaf=is_leaf,
                allow_non_leaf_shell_tests=False,
            )
        except ValueError as exc:
            await self.log_cb(
                "TestGenerator",
                str(exc),
                "error",
                node_id,
            )
            return False
        upsert_node_contract(node_id, self._build_frozen_node_contract(node_id, requirement_data, interfaces, tests))
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
        self._update_node_session(
            node_id,
            {
                "test_plan": self._build_test_plan(tests),
                "test_artifacts": tests,
                "phase_status": {"test": "completed"},
            },
        )

        modified_test_files = extract_modified_files_from_messages(test_messages)
        if modified_test_files:
            await self.log_cb(
                "TestGenerator",
                f"Generated test files: {', '.join(sorted(modified_test_files))}",
                None,
                node_id,
            )
        await self.log_cb(
            "TestGenerator",
            f"Stored {len(tests)} test mapping item(s) into traceability DB.",
            None,
            node_id,
        )
        return True

    async def run_implement_phase(self, node_id: str, requirement_data: dict[str, Any]) -> bool:
        is_leaf = not bool(requirement_data.get("children_ids"))
        session = utils.load_node_session(node_id)
        if not is_leaf and session.get("execution_mode") == "skipped_non_leaf":
            await self.log_cb(
                "Compiler",
                "Skipping IMPLEMENT for non-leaf node because DESIGN marked it as skipped.",
                None,
                node_id,
            )
            return True

        self._update_node_session(
            node_id,
            {
                "phase_status": {"implement": "in_progress"},
            },
        )

        interfaces = get_interfaces_by_req_id(node_id)
        tests = get_tests_by_req_id(node_id)
        if not interfaces:
            await self.log_cb(
                "TestDrivenDeveloper",
                "IMPLEMENT phase requires designed interfaces, but none were found.",
                "error",
                node_id,
            )
            return False

        if not is_leaf:
            design_mode = str(session.get("design_mode", "")).strip()
            shell_test_mode = False
            if design_mode != "non_leaf_full" and tests:
                await self.log_cb(
                    "Compiler",
                    f"Ignoring {len(tests)} stored test record(s) for non-leaf node {node_id}. Non-leaf IMPLEMENT performs only lightweight convergence.",
                    "warning",
                    node_id,
                )
            if design_mode != "non_leaf_full":
                convergence_summary = self._build_non_leaf_convergence_summary(node_id, requirement_data)
                audit_output, audit_messages = await self.interface_designer.audit_non_leaf_connectivity(
                    node_id=node_id,
                    interfaces=interfaces,
                    convergence_summary=convergence_summary,
                )
                audit_modified_files = extract_modified_files_from_messages(audit_messages)
                if audit_modified_files:
                    context_pipeline.cache.invalidate_file_layers(node_id)
                audit_says_no_changes = "NO_CHANGES_NEEDED" in (audit_output or "").upper()

                precheck_output = await self._run_non_leaf_build_verification(node_id)
                precheck_exit_code = parse_test_results(precheck_output).get("exit_code")
                if audit_says_no_changes and precheck_exit_code == 0:
                    await self.log_cb(
                        "Compiler",
                        "Non-leaf IMPLEMENT audit confirmed the parent shell is already connected. Skipping convergence edits.",
                        None,
                        node_id,
                    )
                    update_interface_implemented_status(node_id, True)
                    self._update_node_session(
                        node_id,
                        {
                            "result_state": self._determine_non_leaf_result_state(requirement_data),
                            "phase_status": {"implement": "completed"},
                            "tdd_handoff": {
                                "last_test_type": "non_leaf_convergence",
                                "last_failed_output_summary": "",
                                "modified_files": audit_modified_files,
                                "root_cause_notes": ["Audit reported no changes needed and build verification passed."],
                            },
                        },
                    )
                    return True

                convergence_output, convergence_messages = await self.interface_designer.converge_non_leaf(
                    node_id=node_id,
                    interfaces=interfaces,
                    convergence_summary=convergence_summary,
                )
                modified_files = extract_modified_files_from_messages(convergence_messages)
                if modified_files:
                    context_pipeline.cache.invalidate_file_layers(node_id)
                    await self.log_cb(
                        "Compiler",
                        f"Non-leaf convergence modified files: {', '.join(sorted(modified_files))}",
                        None,
                        node_id,
                    )
                else:
                    await self.log_cb(
                        "Compiler",
                        "Non-leaf convergence completed without code changes.",
                        None,
                        node_id,
                    )
                build_output = await self._run_non_leaf_build_verification(node_id)
                build_exit_code = parse_test_results(build_output).get("exit_code")
                if build_exit_code != 0:
                    await self.log_cb(
                        "Compiler",
                        "Non-leaf convergence build verification failed.",
                        "error",
                        node_id,
                    )
                    if utils.debug_logger:
                        utils.debug_logger.log(f"NON_LEAF_BUILD[{node_id}]", build_output)
                    update_interface_implemented_status(node_id, False)
                    self._update_node_session(
                        node_id,
                        {
                            "phase_status": {"implement": "failed"},
                            "tdd_handoff": {
                                "last_test_type": "non_leaf_convergence",
                                "last_failed_output_summary": self._summarize_batch_output(build_output),
                                "modified_files": modified_files,
                                "root_cause_notes": ["Build verification failed after non-leaf convergence."],
                            },
                        },
                    )
                    return False

                await self.log_cb(
                    "Compiler",
                    "Non-leaf IMPLEMENT completed with lightweight convergence only. Parent-level assembly and build verification passed.",
                    None,
                    node_id,
                )
                update_interface_implemented_status(node_id, True)
                self._update_node_session(
                    node_id,
                    {
                        "result_state": self._determine_non_leaf_result_state(requirement_data),
                        "phase_status": {"implement": "completed"},
                        "tdd_handoff": {
                            "last_test_type": "non_leaf_convergence",
                            "last_failed_output_summary": "",
                            "modified_files": modified_files,
                            "root_cause_notes": ["Parent-level convergence passed build verification."],
                        },
                    },
                )
                return True

        if not tests:
            await self.log_cb(
                "TestDrivenDeveloper",
                "No generated tests were found for this node. Skipping TDD loop and marking the node as passed by definition.",
                node_id,
            )
            update_interface_implemented_status(node_id, True)
            self._update_node_session(
                node_id,
                {
                    "phase_status": {"implement": "completed"},
                    "tdd_handoff": {
                        "last_test_type": "",
                        "last_failed_output_summary": "",
                        "modified_files": [],
                        "root_cause_notes": ["No tests were generated for this node."],
                    },
                },
            )
            return True

        previous_group_handoff_summary = ""
        previous_group_modified_files: list[str] = []
        session = utils.load_node_session(node_id)
        node_understanding = session.get("node_understanding", {})
        interface_spec = session.get("interface_spec", [])
        test_plan = session.get("test_plan", {})

        for test_type in self._get_selected_test_types():
            typed_tests = [test for test in tests if str(test.get("type", "")).strip() == test_type]
            if not typed_tests:
                continue

            test_files = self._collect_test_files(typed_tests)
            run_tests_usage = {"run_tests": 0}

            await self.log_cb(
                "TestDrivenDeveloper",
                f"Implementing against {test_type} tests in {len(test_files)} file(s) with budget {DEFAULT_TDD_TEST_BUDGET}...",
                None,
                node_id,
            )
            messages, tools = self.test_driven_developer.build_initial_messages(
                node_id=node_id,
                test_files=test_files,
                test_type=test_type,
                scenarios=requirement_data.get("scenarios", []),
                current_interfaces=interfaces,
                preloaded_source=context_pipeline.build_incremental_context(node_id, previous_group_modified_files) if previous_group_modified_files else None,
                node_understanding=node_understanding,
                interface_spec=interface_spec,
                test_plan=test_plan,
                previous_failure_summary=previous_group_handoff_summary,
            )
            implement_output, implement_messages = await self.test_driven_developer.run_from_messages(
                messages=messages,
                node_id=node_id,
                max_steps=50,
                tools=tools,
                run_tests_budget=DEFAULT_TDD_TEST_BUDGET,
                run_tests_usage=run_tests_usage,
                stop_on_test_budget_exhausted=True,
                run_tests_executor=lambda node_id=node_id, test_type=test_type, test_files=list(test_files): self._run_test_batch_for_agent(
                    node_id=node_id,
                    test_type=test_type,
                    test_files=test_files,
                ),
            )

            context_pipeline.cache.invalidate_file_layers(node_id)
            self._store_new_interfaces_from_tdd_output(node_id, implement_output)
            latest_run_tests_result = self.test_driven_developer.get_last_run_tests_result()
            modified_files = extract_modified_files_from_messages(implement_messages)

            if "IMPLEMENTED" not in (implement_output or "").upper():
                await self.log_cb(
                    "TestDrivenDeveloper",
                    f"{test_type} group ended without explicit IMPLEMENTED. Continuing with system verification.",
                    None,
                    node_id,
                )

            if latest_run_tests_result:
                group_passed, group_statuses = self._map_statuses_from_batch_output(
                    tests=typed_tests,
                    batch_output=latest_run_tests_result,
                )
            else:
                group_passed = False
                group_statuses = {
                    str(test.get("test_id", "")).strip(): False
                    for test in typed_tests
                    if str(test.get("test_id", "")).strip()
                }
                await self.log_cb(
                    "Compiler",
                    f"{test_type} group ended without any run_tests result. Marking the current batch as not passed.",
                    "error",
                    node_id,
                )

            update_test_pass_statuses(group_statuses)
            previous_group_handoff_summary = self._build_group_handoff_summary(
                node_id=node_id,
                test_type=test_type,
                modified_files=modified_files,
                group_statuses=group_statuses,
                latest_run_tests_result=latest_run_tests_result,
            )
            previous_group_modified_files = modified_files
            self._update_node_session(
                node_id,
                {
                    "tdd_handoff": {
                        "last_test_type": test_type,
                        "last_failed_output_summary": "" if group_passed else self._summarize_batch_output(latest_run_tests_result or ""),
                        "modified_files": modified_files,
                        "root_cause_notes": (
                            [f"{test_type} batch passed from the latest run_tests result."]
                            if group_passed
                            else [f"{test_type} batch still has failing tests and requires another TDD pass."]
                        ),
                    },
                },
            )

            if group_passed:
                await self.log_cb(
                    "Compiler",
                    f"{test_type} group passed from the latest run_tests result.",
                    None,
                    node_id,
                )
            else:
                await self.log_cb(
                    "Compiler",
                    f"{test_type} group did not pass from the latest run_tests result.",
                    "error",
                    node_id,
                )

        final_statuses = {
            str(test.get("test_id", "")).strip(): test.get("passed")
            for test in get_tests_by_req_id(node_id)
            if str(test.get("test_id", "")).strip()
        }
        final_ok = bool(final_statuses) and all(value is True for value in final_statuses.values())

        update_interface_implemented_status(node_id, final_ok)
        self._update_node_session(
            node_id,
            {
                "phase_status": {"implement": "completed" if final_ok else "failed"},
            },
        )
        return final_ok

    async def _run_non_leaf_build_verification(self, node_id: str) -> str:
        from agents.tools.cli_tools import run_build_impl

        await self.log_cb(
            "Compiler",
            "Running non-leaf convergence build verification...",
            None,
            node_id,
        )
        return await run_build_impl()

    def _store_new_interfaces_from_tdd_output(self, node_id: str, implement_output: str) -> None:
        interfaces = extract_json_array_from_markdown(implement_output)
        if not interfaces:
            return
        self._store_interfaces(node_id, interfaces)
        context_pipeline.cache.invalidate_db_layers(node_id)

    def _store_interfaces(self, node_id: str, interfaces: list[dict[str, Any]]) -> None:
        for interface in interfaces:
            if not isinstance(interface, dict):
                continue

            interface_id = str(interface.get("interface_id", "")).strip()
            if not interface_id:
                continue

            reuse = bool(interface.get("reuse"))
            existing = get_interface_by_id(interface_id)
            req_ids = self._merge_req_ids(existing, node_id)

            if reuse and existing:
                update_interface_req_ids(interface_id, node_id)
            else:
                insert_interface(
                    interface_id=interface_id,
                    req_ids=req_ids,
                    type=str(interface.get("type", "")).strip(),
                    content=json.dumps(interface, ensure_ascii=False),
                    file_path=str(interface.get("file_path", "")).strip(),
                    first_line=str(interface.get("first_line", "")).strip(),
                    implemented=bool(existing.get("implemented")) if existing else False,
                    callers=self._normalize_string_list(interface.get("callers")),
                    callees=self._normalize_string_list(interface.get("callees")),
                )

            self._register_interface_edges(node_id, interface_id, interface)

    def _store_tests(
        self,
        node_id: str,
        tests: list[dict[str, Any]],
        is_leaf: bool,
        allow_non_leaf_shell_tests: bool = False,
    ) -> None:
        generated_ids: set[str] = set()
        sequence = 0

        for test in tests:
            if not isinstance(test, dict):
                continue

            raw_test_id = str(test.get("test_id", "")).strip()
            if not raw_test_id:
                continue

            test_type = str(test.get("type", "")).strip()
            file_path = str(test.get("file_path", "")).strip()
            if not is_leaf and not allow_non_leaf_shell_tests:
                raise ValueError(
                    f"Generated test `{raw_test_id}` is invalid for non-leaf node `{node_id}`. "
                    "Non-leaf nodes should not register tests; they only keep shared interface and aggregation artifacts."
                )
            if not is_leaf and allow_non_leaf_shell_tests and test_type not in {"Integration", "E2E"}:
                raise ValueError(
                    f"Generated non-leaf shell test `{raw_test_id}` has invalid type `{test_type}`. "
                    "Non-leaf shell verification may only use Integration or E2E tests."
                )
            validation_error = self.app_handler.validate_test_path(test_type, file_path)
            if validation_error:
                raise ValueError(
                    f"Generated test `{raw_test_id}` has an invalid path. {validation_error}"
                )

            sequence += 1
            test_id = self._canonicalize_test_id(
                node_id=node_id,
                test_type=test_type,
                raw_test_id=raw_test_id,
                sequence=sequence,
            )
            if test_id in generated_ids:
                raise ValueError(
                    f"Generated duplicate canonical test id `{test_id}` for node `{node_id}`. "
                    "Each stored test must be globally unique."
                )
            generated_ids.add(test_id)

            insert_test(
                test_id=test_id,
                req_id=node_id,
                interface_ids=self._normalize_string_list(test.get("interface_ids")),
                type=test_type,
                file_path=file_path,
                first_line=str(test.get("first_line", "")).strip(),
                passed=None,
            )

    def _register_interface_edges(self, node_id: str, interface_id: str, interface: dict[str, Any]) -> None:
        for caller_id in self._normalize_string_list(interface.get("callers")):
            caller = get_interface_by_id(caller_id)
            if not caller:
                continue
            for source_req_id in caller.get("req_ids", []):
                if source_req_id and source_req_id != node_id:
                    insert_call_edge(
                        source_req_id=source_req_id,
                        target_req_id=node_id,
                        from_interface_id=caller_id,
                        to_interface_id=interface_id,
                        edge_type="cross_req",
                    )

        for callee_id in self._normalize_string_list(interface.get("callees")):
            callee = get_interface_by_id(callee_id)
            if not callee:
                continue
            for target_req_id in callee.get("req_ids", []):
                if target_req_id and target_req_id != node_id:
                    insert_call_edge(
                        source_req_id=node_id,
                        target_req_id=target_req_id,
                        from_interface_id=interface_id,
                        to_interface_id=callee_id,
                        edge_type="cross_req",
                    )

    def _canonicalize_test_id(
        self,
        node_id: str,
        test_type: str,
        raw_test_id: str,
        sequence: int,
    ) -> str:
        sanitized_node_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(node_id or "").strip()) or "NODE"
        sanitized_type = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(test_type or "").strip()) or "TEST"
        sanitized_raw = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(raw_test_id or "").strip()) or "TEST"
        return f"{sanitized_node_id}::{sanitized_type}::{sequence:03d}::{sanitized_raw}"

    def _build_group_handoff_summary(
        self,
        node_id: str,
        test_type: str,
        modified_files: list[str],
        group_statuses: dict[str, bool | None],
        latest_run_tests_result: str | None,
    ) -> str:
        passed_tests = sorted(test_id for test_id, status in group_statuses.items() if status is True)
        failed_tests = sorted(test_id for test_id, status in group_statuses.items() if status is not True)
        summary_lines = [
            f"- Previous group: {test_type}",
            f"- Modified files: {', '.join(sorted(modified_files)) if modified_files else 'none'}",
            f"- Tests passed in previous group: {', '.join(passed_tests[:12]) if passed_tests else 'none'}",
            f"- Remaining failing tests from previous group: {', '.join(failed_tests[:12]) if failed_tests else 'none'}",
        ]
        contract_row = get_node_contract(node_id)
        contract = contract_row.get("content", {}) if isinstance(contract_row, dict) else {}
        canonical_routes = contract.get("canonical_routes") or []
        if canonical_routes:
            summary_lines.append(f"- Canonical routes for this node: {', '.join(canonical_routes[:10])}")
        auth_expectation = contract.get("auth_expectation", "")
        if auth_expectation:
            summary_lines.append(f"- Auth expectation: {auth_expectation}")
        return "\n".join(summary_lines)

    def _build_frozen_node_contract(
        self,
        node_id: str,
        requirement_data: dict[str, Any],
        interfaces: list[dict[str, Any]],
        tests: list[dict[str, Any]],
    ) -> dict[str, Any]:
        is_leaf = not bool(requirement_data.get("children_ids"))
        interface_summaries = []
        canonical_routes: list[str] = []
        shared_shell_targets: list[str] = []
        provider_hints: list[str] = []
        auth_expectation = "unspecified"

        for interface in interfaces:
            if not isinstance(interface, dict):
                continue
            file_path = str(interface.get("file_path", "")).strip()
            first_line = str(interface.get("first_line", "")).strip()
            description = str(interface.get("description", "") or "")
            iface_type = str(interface.get("type", "")).strip()
            interface_summaries.append(
                {
                    "interface_id": str(interface.get("interface_id", "")).strip(),
                    "type": iface_type,
                    "file_path": file_path,
                    "first_line": first_line,
                }
            )

            for candidate in re.findall(r'["\'](/[^"\']+)["\']', f"{first_line}\n{description}\n{file_path}"):
                if candidate not in canonical_routes:
                    canonical_routes.append(candidate)

            normalized_path = file_path.replace("\\", "/")
            if any(token in normalized_path for token in ("app.", "/app.", "main.", "/main.", "routes/", "router", "layout", "provider")):
                if file_path and file_path not in shared_shell_targets:
                    shared_shell_targets.append(file_path)

            if "provider" in normalized_path.lower() or "provider" in first_line.lower():
                if file_path and file_path not in provider_hints:
                    provider_hints.append(file_path)

        for test in tests:
            if not isinstance(test, dict):
                continue
            file_path = str(test.get("file_path", "")).strip()
            first_line = str(test.get("first_line", "")).strip()
            for candidate in re.findall(r'["\'](/[^"\']+)["\']', f"{file_path}\n{first_line}"):
                if candidate not in canonical_routes:
                    canonical_routes.append(candidate)

        requirement_blob = json.dumps(requirement_data, ensure_ascii=False).lower()
        if "login" in requirement_blob or "authenticated" in requirement_blob or "logout" in requirement_blob:
            auth_expectation = "auth-sensitive"
        if "without login" in requirement_blob or "unauthenticated" in requirement_blob:
            auth_expectation = "explicit-unauthenticated-flow"

        if not shared_shell_targets and not is_leaf:
            for fallback in ("frontend/src/App.tsx", "frontend/src/main.tsx", "backend/src/app.js"):
                shared_shell_targets.append(fallback)

        return {
            "req_id": node_id,
            "node_role": "leaf" if is_leaf else "non_leaf",
            "children_ids": requirement_data.get("children_ids") or [],
            "interface_count": len(interface_summaries),
            "interfaces": interface_summaries,
            "test_files": sorted(
                {
                    str(test.get("file_path", "")).strip()
                    for test in tests
                    if isinstance(test, dict) and str(test.get("file_path", "")).strip()
                }
            ),
            "canonical_routes": canonical_routes[:20],
            "auth_expectation": auth_expectation,
            "provider_hints": provider_hints[:10],
            "shared_shell_targets": shared_shell_targets[:12],
            "assembly_scope": (
                [
                    "app shell",
                    "router / route container",
                    "top-level layout / page container",
                    "shared provider composition",
                    "child mounting points",
                ]
                if not is_leaf else
                ["leaf feature implementation"]
            ),
        }

    def _build_non_leaf_convergence_summary(self, node_id: str, requirement_data: dict[str, Any]) -> str:
        child_ids = [str(child_id).strip() for child_id in (requirement_data.get("children_ids") or []) if str(child_id).strip()]
        if not child_ids:
            return "- No child nodes were found."

        lines = [
            "- This parent node should converge child capabilities into one coherent subsystem.",
            "- Use concrete child outputs as assembly inputs: implemented interfaces, landed files, passed tests, and remaining failures.",
        ]
        contract_row = get_node_contract(node_id)
        contract = contract_row.get("content", {}) if isinstance(contract_row, dict) else {}
        assembly_scope = contract.get("assembly_scope") or []
        shared_shell_targets = contract.get("shared_shell_targets") or []
        provider_hints = contract.get("provider_hints") or []
        canonical_routes = contract.get("canonical_routes") or []
        auth_expectation = contract.get("auth_expectation", "")
        if assembly_scope:
            lines.append(f"- Parent assembly scope: {', '.join(assembly_scope)}")
        if shared_shell_targets:
            lines.append(f"- Parent shared shell targets: {', '.join(shared_shell_targets[:10])}")
        if provider_hints:
            lines.append(f"- Parent provider hints: {', '.join(provider_hints[:10])}")
        if canonical_routes:
            lines.append(f"- Parent canonical routes: {', '.join(canonical_routes[:12])}")
        if auth_expectation:
            lines.append(f"- Parent auth expectation: {auth_expectation}")
        lines.append("- Parent convergence must not duplicate providers, invent fake user/session fallbacks, or override child feature semantics.")
        for child_id in child_ids:
            child_interfaces = get_interfaces_by_req_id(child_id)
            child_tests = get_tests_by_req_id(child_id)
            implemented = sorted(
                str(interface.get("interface_id", "")).strip()
                for interface in child_interfaces
                if interface.get("implemented")
            )
            child_files = sorted(
                {
                    str(interface.get("file_path", "")).strip()
                    for interface in child_interfaces
                    if str(interface.get("file_path", "")).strip()
                }
            )
            passed_tests = sorted(
                str(test.get("test_id", "")).strip()
                for test in child_tests
                if test.get("passed") is True
            )
            failed_tests = sorted(
                str(test.get("test_id", "")).strip()
                for test in child_tests
                if test.get("passed") is False
            )
            lines.append(f"- Child `{child_id}` implemented interfaces: {', '.join(implemented[:10]) if implemented else 'none'}")
            lines.append(f"- Child `{child_id}` landed files: {', '.join(child_files[:10]) if child_files else 'none'}")
            lines.append(f"- Child `{child_id}` passed tests: {', '.join(passed_tests[:10]) if passed_tests else 'none'}")
            if failed_tests:
                lines.append(f"- Child `{child_id}` remaining failed tests: {', '.join(failed_tests[:10])}")
        return "\n".join(lines)

    def _map_statuses_from_batch_output(
        self,
        tests: list[dict[str, Any]],
        batch_output: str,
    ) -> tuple[bool, dict[str, bool | None]]:
        grouped_tests: dict[str, list[dict[str, Any]]] = {}
        for test in tests:
            file_path = str(test.get("file_path", "")).strip()
            if file_path:
                grouped_tests.setdefault(file_path, []).append(test)

        parsed_result = parse_test_results(batch_output)
        batch_passed = parsed_result.get("exit_code") == 0
        file_batch_statuses = self._extract_file_batch_statuses(parsed_result)
        status_by_test_id: dict[str, bool | None] = {}
        all_passed = True

        for file_path, file_tests in grouped_tests.items():
            normalized_file_path = file_path.replace("\\", "/")
            file_passed = file_batch_statuses.get(normalized_file_path, batch_passed)
            file_statuses = self._map_file_test_statuses(file_tests, parsed_result, file_passed)
            status_by_test_id.update(file_statuses)
            if not all(value is True for value in file_statuses.values()):
                all_passed = False

        return all_passed, status_by_test_id

    async def _run_test_batch_for_agent(
        self,
        node_id: str,
        test_type: str,
        test_files: list[str],
    ) -> str:
        if not test_files:
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                f"No test files were configured for the current {test_type} batch of node {node_id}.\n"
            )

        for file_path in test_files:
            await self.log_cb(
                "Compiler",
                f"System is executing current {test_type} batch file: {file_path}",
                None,
                node_id,
            )
        raw_output = await self.app_handler.run_test_group(test_type, test_files)
        return self._prepend_agent_batch_summary(node_id, test_type, test_files, raw_output)

    def _prepend_agent_batch_summary(
        self,
        node_id: str,
        test_type: str,
        test_files: list[str],
        raw_output: str,
    ) -> str:
        parsed = parse_test_results(raw_output)
        lines: list[str] = []

        grouped_sub_batches = self._group_sub_batches_by_requested_file(parsed)
        for file_path in test_files:
            normalized_file_path = file_path.replace("\\", "/")
            sub_batch = grouped_sub_batches.get(normalized_file_path)
            lines.append(f"Test File: {file_path}")
            lines.append("Test Results:")
            if not sub_batch:
                lines.append(raw_output.rstrip())
                lines.append("")
                continue
            lines.append(str(sub_batch.get("raw_output", "")).rstrip())
            lines.append("")

        if not grouped_sub_batches:
            return raw_output

        return "\n".join(lines).rstrip()

    def _map_file_test_statuses(
        self,
        file_tests: list[dict[str, Any]],
        parsed_result: dict[str, Any],
        file_passed: bool,
    ) -> dict[str, bool | None]:
        return {
            str(test.get("test_id", "")).strip(): file_passed
            for test in file_tests
            if str(test.get("test_id", "")).strip()
        }

    @staticmethod
    def _extract_file_batch_statuses(parsed_result: dict[str, Any]) -> dict[str, bool]:
        status_by_file: dict[str, bool] = {}
        for sub_batch in parsed_result.get("sub_batches", []) or []:
            if not isinstance(sub_batch, dict):
                continue
            sub_batch_passed = int(sub_batch.get("exit_code", 1)) == 0
            for file_path in sub_batch.get("requested_files", []) or []:
                normalized = str(file_path or "").strip().replace("\\", "/")
                if normalized:
                    status_by_file[normalized] = sub_batch_passed
        return status_by_file

    @staticmethod
    def _group_sub_batches_by_requested_file(parsed_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for sub_batch in parsed_result.get("sub_batches", []) or []:
            if not isinstance(sub_batch, dict):
                continue
            for file_path in sub_batch.get("requested_files", []) or []:
                normalized = str(file_path or "").strip().replace("\\", "/")
                if normalized:
                    grouped[normalized] = sub_batch
            if not sub_batch.get("requested_files"):
                raw_output = str(sub_batch.get("raw_output", "")).strip()
                match = re.search(r"Requested Test File:\s*(.+)", raw_output)
                if match:
                    normalized = match.group(1).strip().replace("\\", "/")
                    if normalized:
                        grouped[normalized] = sub_batch
        return grouped

    @staticmethod
    def _collect_test_files(tests: list[dict[str, Any]]) -> list[str]:
        seen: list[str] = []
        for test in tests:
            file_path = str(test.get("file_path", "")).strip()
            if file_path and file_path not in seen:
                seen.append(file_path)
        return seen

    @staticmethod
    def _merge_req_ids(existing: dict[str, Any] | None, node_id: str) -> list[str]:
        req_ids = list(existing.get("req_ids", [])) if existing else []
        if node_id not in req_ids:
            req_ids.append(node_id)
        return req_ids

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result
