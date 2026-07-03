import json
import os
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
    get_tests_by_req_id,
    insert_call_edge,
    insert_interface,
    insert_test,
    upsert_node_contract,
    update_interface_implemented,
    update_interface_implemented_status,
    update_interface_req_ids,
    update_test_pass_statuses,
)
from utils import extract_json_array_from_markdown, extract_modified_files_from_messages
from workflow_phase_utils import (
    DEFAULT_TDD_TEST_BUDGET,
    build_base_node_session,
    build_frozen_node_contract,
    build_group_handoff_summary,
    build_non_leaf_convergence_summary,
    build_non_leaf_scope_note,
    build_test_plan,
    canonicalize_test_id,
    classify_non_leaf_work,
    collect_test_files,
    determine_non_leaf_result_state,
    get_non_leaf_gate_failures,
    get_selected_test_types,
    map_statuses_from_batch_output,
    merge_req_ids,
    normalize_string_list,
    prepend_agent_batch_summary,
    summarize_batch_output,
)


class WorkflowPhaseRunner:
    """Run the DESIGN and IMPLEMENT phases while the workflow manager stays orchestration-focused."""

    def __init__(
        self,
        workspace_path: str,
        requirement_path: str,
        app_type: str,
        interface_designer,
        test_generator,
        test_driven_developer,
        log_cb: Callable[[str, str, str | None, str | None], Awaitable[None] | None],
    ):
        self.workspace_path = workspace_path
        self.requirement_path = requirement_path
        self.app_type = app_type
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
        if "interfaces" in patch:
            legacy_removed = False
            for legacy_key in ("interface_ir", "materialized_interfaces"):
                if legacy_key in session:
                    session.pop(legacy_key, None)
                    legacy_removed = True
            if legacy_removed:
                utils.save_node_session(node_id, session)
        context_pipeline.cache.invalidate(node_id, "node_session")
        context_pipeline.cache.invalidate(node_id, "recent_failure_summary")
        return session

    # ----- Shared workflow helpers -----

    async def _run_tdd_batches_for_node(
        self,
        node_id: str,
        tests: list[dict[str, Any]],
        scope_note: str = "",
        initial_failure_summary: str = "",
    ) -> bool:
        previous_group_handoff_summary = initial_failure_summary.strip()
        previous_group_modified_files: list[str] = []

        for test_type in get_selected_test_types():
            typed_tests = [test for test in tests if str(test.get("type", "")).strip() == test_type]
            if not typed_tests:
                continue

            test_files = collect_test_files(typed_tests)
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
                preloaded_source=context_pipeline.build_incremental_context(node_id, previous_group_modified_files) if previous_group_modified_files else None,
                previous_failure_summary=previous_group_handoff_summary,
                scope_note=scope_note,
            )
            implement_output, implement_messages = await self.test_driven_developer.run_from_messages(
                messages=messages,
                node_id=node_id,
                max_steps=75,
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
                group_passed, group_statuses = map_statuses_from_batch_output(
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
            previous_group_handoff_summary = build_group_handoff_summary(
                node_id=node_id,
                test_type=test_type,
                modified_files=modified_files,
                group_statuses=group_statuses,
            )
            previous_group_modified_files = modified_files
            self._update_node_session(
                node_id,
                {
                    "tdd_handoff": {
                        "last_test_type": test_type,
                        "last_failed_output_summary": "" if group_passed else summarize_batch_output(latest_run_tests_result or ""),
                        "modified_files": modified_files,
                        "root_cause_notes": (
                            [f"{test_type} batch passed from the latest run_tests result."]
                            if group_passed
                            else [f"{test_type} batch still has failing tests and requires another TDD pass."]
                        ),
                    },
                    "recent_failure_summary": "" if group_passed else summarize_batch_output(latest_run_tests_result or ""),
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
        return bool(final_statuses) and all(value is True for value in final_statuses.values())

    async def _run_test_batch_for_agent(
        self,
        node_id: str,
        test_type: str,
        test_files: list[str],
    ) -> str:
        if not test_files:
            return (
                "Exit Code: 0\n"
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
        return prepend_agent_batch_summary(test_files, raw_output)

    # ----- Design phase -----

    async def run_design_phase(self, node_id: str, requirement_data: dict[str, Any]) -> bool:
        is_leaf = not bool(requirement_data.get("children_ids"))
        design_mode = "leaf_full" if is_leaf else classify_non_leaf_work(requirement_data)
        self._update_node_session(
            node_id,
            build_base_node_session(
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
            f"Running unified design session for `{design_mode}`: analyze requirement, capture parent constraints, design interfaces, and materialize owned code...",
            None,
            node_id,
        )
        design_bundle, design_messages = await self.interface_designer.design_bundle(
            node_id=node_id,
            requirement_data=requirement_data,
            design_mode=design_mode,
        )
        subtree_invariants = design_bundle.get("subtree_invariants", [])
        assembly_boundaries = design_bundle.get("assembly_boundaries", [])
        interfaces = design_bundle.get("interfaces", [])
        interface_spec = design_bundle.get("interface_spec", [])

        if not interfaces:
            role_label = "leaf" if is_leaf else "non-leaf"
            await self.log_cb(
                "InterfaceDesigner",
                f"Unified design session for the current {role_label} node did not return a valid interface JSON array.",
                "error",
                node_id,
            )
            return False
        if interfaces and not interface_spec:
            interface_spec = [
                {
                    "interface_id": str(interface.get("interface_id", "")).strip(),
                    "type": str(interface.get("type", "")).strip(),
                    "file_path": str(interface.get("file_path", "")).strip(),
                    "first_line": str(interface.get("first_line", "")).strip(),
                    "responsibility": str(interface.get("responsibility", "") or "").strip(),
                    "specification": str(interface.get("specification", "") or "").strip(),
                    "test_focus": interface.get("test_focus", []) if isinstance(interface.get("test_focus"), list) else [],
                    "reuse_notes": interface.get("reuse_notes", []) if isinstance(interface.get("reuse_notes"), list) else [],
                }
                for interface in interfaces
                if isinstance(interface, dict)
            ]

        self._update_node_session(node_id, {"phase_status": {"understand": "completed"}})
        await self.log_cb(
            "InterfaceDesigner",
            "Completed unified design session and persisted node session context.",
            None,
            node_id,
        )

        clear_node_design_artifacts(node_id)
        frozen_contract = build_frozen_node_contract(node_id, requirement_data, interfaces, [])
        upsert_node_contract(node_id, frozen_contract)
        if interfaces:
            self._store_interfaces(node_id, interfaces)
            context_pipeline.cache.invalidate_db_layers(node_id)
        self._update_node_session(
            node_id,
            {
                "subtree_invariants": subtree_invariants,
                "assembly_boundaries": assembly_boundaries,
                "parent_contract": frozen_contract if not is_leaf else {},
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
            (
                f"Stored {len(interfaces)} interface definition(s) into traceability DB."
                if interfaces
                else "Stored non-leaf invariants and assembly boundaries without registering new interfaces."
            ),
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
                "interfaces": materialized,
                "materialized_files": materialized_files,
            },
        )
        await self.log_cb(
            "InterfaceDesigner",
            (
                f"Materialized {len(interfaces)} interface(s) into code during the unified design session."
                if interfaces
                else "No parent-owned interfaces needed materialization in the unified design session."
            ),
            None,
            node_id,
        )

        await self.log_cb(
            "TestGenerator",
            (
                "Generating unit, integration, and end-to-end tests from the designed interfaces..."
                if is_leaf else
                "Generating parent integration and end-to-end validation tests from the parent contract and shell interfaces..."
            ),
            None,
            node_id,
        )
        messages, tools = self.test_generator.build_initial_messages(
            node_id=node_id,
            requirement_data=requirement_data,
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
                allow_non_leaf_shell_tests=not is_leaf,
            )
        except ValueError as exc:
            await self.log_cb(
                "TestGenerator",
                str(exc),
                "error",
                node_id,
            )
            return False
        upsert_node_contract(node_id, build_frozen_node_contract(node_id, requirement_data, interfaces, tests))
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
        self._update_node_session(
            node_id,
            {
                "execution_mode": "leaf_full" if is_leaf else "non_leaf_parent_contract",
                "test_plan": build_test_plan(tests),
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

    # ----- Implementation phase -----

    async def run_implement_phase(self, node_id: str, requirement_data: dict[str, Any]) -> bool:
        is_leaf = not bool(requirement_data.get("children_ids"))
        session = utils.load_node_session(node_id)
        if not is_leaf and session.get("execution_mode") == "skipped_non_leaf":
            await self.log_cb(
                "Compiler",
                "Skipping IMPLEMENT for non-leaf node because DESIGN skipped this node entirely.",
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

        if not tests:
            await self.log_cb(
                "TestDrivenDeveloper",
                "No generated tests were found for this node. IMPLEMENT cannot complete without generated test artifacts.",
                "error",
                node_id,
            )
            self._update_node_session(
                node_id,
                {
                    "phase_status": {"implement": "failed"},
                    "tdd_handoff": {
                        "last_test_type": "",
                        "last_failed_output_summary": "",
                        "modified_files": [],
                        "root_cause_notes": ["No tests were generated for this node."],
                    },
                },
            )
            return False

        if not is_leaf:
            blocking_children = get_non_leaf_gate_failures(requirement_data)
            if blocking_children:
                await self.log_cb(
                    "Compiler",
                    "Parent done gate failed. Non-leaf IMPLEMENT requires every child node to finish successfully before parent integration validation can pass. Blocking children: "
                    + ", ".join(blocking_children),
                    "error",
                    node_id,
                )
                self._update_node_session(
                    node_id,
                    {
                        "phase_status": {"implement": "failed"},
                        "tdd_handoff": {
                            "last_test_type": "parent_done_gate",
                            "last_failed_output_summary": "",
                            "modified_files": [],
                            "root_cause_notes": [f"Blocking child states: {', '.join(blocking_children)}"],
                        },
                    },
                )
                update_interface_implemented_status(node_id, False)
                return False

            design_mode = str(session.get("design_mode", "")).strip()
            if design_mode not in {"non_leaf_full", "non_leaf_ui_only"}:
                await self.log_cb(
                    "Compiler",
                    f"Unsupported non-leaf design mode `{design_mode}` for IMPLEMENT.",
                    "error",
                    node_id,
                )
                self._update_node_session(node_id, {"phase_status": {"implement": "failed"}})
                return False

            convergence_summary = build_non_leaf_convergence_summary(node_id, requirement_data)
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

            modified_files = list(audit_modified_files)
            if not audit_says_no_changes or precheck_exit_code != 0:
                _, convergence_messages = await self.interface_designer.converge_non_leaf(
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
                                "last_test_type": "non_leaf_build_verification",
                                "last_failed_output_summary": summarize_batch_output(build_output),
                                "modified_files": modified_files,
                                "root_cause_notes": ["Build verification failed after parent integration convergence."],
                            },
                        },
                    )
                    return False
            else:
                await self.log_cb(
                    "Compiler",
                    "Non-leaf audit found the parent shell already wired. Proceeding directly to parent integration and browser validation.",
                    None,
                    node_id,
                )

            final_ok = await self._run_tdd_batches_for_node(
                node_id=node_id,
                tests=tests,
                scope_note=build_non_leaf_scope_note(),
                initial_failure_summary=convergence_summary,
            )
            update_interface_implemented_status(node_id, final_ok)
            self._update_node_session(
                node_id,
                {
                    "result_state": determine_non_leaf_result_state(requirement_data) if final_ok else "",
                    "phase_status": {"implement": "completed" if final_ok else "failed"},
                },
            )
            return final_ok

        final_ok = await self._run_tdd_batches_for_node(
            node_id=node_id,
            tests=tests,
        )
        update_interface_implemented_status(node_id, final_ok)
        self._update_node_session(
            node_id,
            {
                "phase_status": {"implement": "completed" if final_ok else "failed"},
            },
        )
        return final_ok

    # ----- Persistence and execution helpers -----

    async def _run_non_leaf_build_verification(self, node_id: str) -> str:
        from agents.tools.cli_tools import run_build_impl

        await self.log_cb(
            "Compiler",
            "Running parent integration build verification...",
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
            req_ids = merge_req_ids(existing, node_id)

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
                    callers=normalize_string_list(interface.get("callers")),
                    callees=normalize_string_list(interface.get("callees")),
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
            test_id = canonicalize_test_id(
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
                interface_ids=normalize_string_list(test.get("interface_ids")),
                type=test_type,
                file_path=file_path,
                first_line=str(test.get("first_line", "")).strip(),
                passed=None,
            )

    def _register_interface_edges(self, node_id: str, interface_id: str, interface: dict[str, Any]) -> None:
        for caller_id in normalize_string_list(interface.get("callers")):
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

        for callee_id in normalize_string_list(interface.get("callees")):
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
