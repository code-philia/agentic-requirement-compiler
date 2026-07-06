import json
import os
from typing import Any, Awaitable, Callable

import utils
from agents.context_pipeline import context_pipeline
from app_types import create_app_type_handler
from runtime_sdk import get_runtime
from utils import extract_json_array_from_markdown, extract_modified_files_from_messages
from workflow_phase_utils import (
    DEFAULT_TDD_TEST_BUDGET,
    build_base_node_session,
    build_group_handoff_summary,
    build_test_plan,
    canonicalize_test_id,
    classify_non_leaf_work,
    collect_test_files,
    get_selected_test_types,
    map_statuses_from_batch_output,
    merge_req_ids,
    normalize_string_list,
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

    @property
    def traceability(self):
        return get_runtime().traceability

    def _update_node_session(self, node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        session = utils.merge_node_session(node_id, patch)
        deprecated_keys = (
            "interface_ir",
            "materialized_interfaces",
            "subtree_invariants",
            "assembly_boundaries",
        )
        removed_deprecated = False
        for deprecated_key in deprecated_keys:
            if deprecated_key in session:
                session.pop(deprecated_key, None)
                removed_deprecated = True
        if removed_deprecated:
            utils.save_node_session(node_id, session)
        context_pipeline.cache.invalidate(node_id, "node_session")
        context_pipeline.cache.invalidate(node_id, "recent_failure_summary")
        return session

    @staticmethod
    def _build_design_summary_payload(
        interfaces: list[dict[str, Any]],
        materialized_files: list[str],
    ) -> dict[str, Any]:
        type_counts: dict[str, int] = {}
        items: list[dict[str, Any]] = []
        for interface in interfaces:
            if not isinstance(interface, dict):
                continue
            interface_type = str(interface.get("type", "")).strip() or "Unknown"
            type_counts[interface_type] = type_counts.get(interface_type, 0) + 1
            items.append(
                {
                    "id": str(interface.get("interface_id", "")).strip(),
                    "type": interface_type,
                    "path": str(interface.get("file_path", "")).strip(),
                    "reuse": bool(interface.get("reuse")),
                    "name": str(interface.get("name", "")).strip(),
                }
            )
        return {
            "kind": "design",
            "total": len(items),
            "type_counts": type_counts,
            "files": sorted(materialized_files),
            "items": items,
        }

    @staticmethod
    def _build_test_summary_payload(
        tests: list[dict[str, Any]],
        generated_files: list[str],
    ) -> dict[str, Any]:
        type_counts: dict[str, int] = {}
        items: list[dict[str, Any]] = []
        for test in tests:
            if not isinstance(test, dict):
                continue
            test_type = str(test.get("type", "")).strip() or "Unknown"
            type_counts[test_type] = type_counts.get(test_type, 0) + 1
            items.append(
                {
                    "id": str(test.get("test_id", "")).strip(),
                    "type": test_type,
                    "path": str(test.get("file_path", "")).strip(),
                    "interfaces": normalize_string_list(test.get("interface_ids"))[:3],
                }
            )
        return {
            "kind": "tests",
            "total": len(items),
            "type_counts": type_counts,
            "files": sorted(generated_files),
            "items": items,
        }

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
            latest_verifier_report = self.test_driven_developer.get_last_verifier_report()
            modified_files = extract_modified_files_from_messages(implement_messages)
            if modified_files:
                await self.log_cb(
                    "TestDrivenDeveloper",
                    f"Modified files after {test_type}: {', '.join(sorted(modified_files))}",
                    None,
                    node_id,
                )

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

            self.traceability.set_test_pass_statuses(group_statuses)
            failure_summary = ""
            if not group_passed:
                raw_failure_summary = summarize_batch_output(latest_run_tests_result or "")
                failure_summary = latest_verifier_report.strip() or raw_failure_summary
                if latest_verifier_report.strip() and raw_failure_summary:
                    failure_summary = (
                        latest_verifier_report.strip()
                        + "\n\n<latest_test_output_excerpt>\n"
                        + raw_failure_summary
                        + "\n</latest_test_output_excerpt>"
                    )
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
                        "last_failed_output_summary": "" if group_passed else failure_summary,
                        "modified_files": modified_files,
                        "root_cause_notes": (
                            [f"{test_type} batch passed from the latest run_tests result."]
                            if group_passed
                            else [f"{test_type} batch still has failing tests and requires another TDD pass."]
                        ),
                    },
                    "recent_failure_summary": "" if group_passed else failure_summary,
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
            for test in self.traceability.list_tests(req_id=node_id)
            if str(test.get("test_id", "")).strip()
        }
        if final_statuses:
            passed_count = sum(1 for value in final_statuses.values() if value is True)
            total_count = len(final_statuses)
            await self.log_cb(
                "Compiler",
                f"Test summary: {passed_count}/{total_count} generated tests passed.",
                None,
                node_id,
            )
            if passed_count == total_count:
                await self.log_cb("Compiler", "All generated tests passed.", None, node_id)
            else:
                await self.log_cb("Compiler", "Some generated tests are still failing.", "error", node_id)
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
        return raw_output

    # ----- Design phase -----

    async def run_design_phase(self, node_id: str, requirement_data: dict[str, Any]) -> bool:
        is_leaf = not bool(requirement_data.get("children_ids"))
        design_mode = "leaf_full" if is_leaf else classify_non_leaf_work(requirement_data)
        self._update_node_session(
            node_id,
            build_base_node_session(
                node_id=node_id,
                requirement_data=requirement_data,
                design_mode=design_mode,
            ),
        )

        # ---------------- Interface design ----------------

        if not is_leaf and design_mode == "skip":
            self._update_node_session(
                node_id,
                {
                    "execution_mode": "skipped_non_leaf",
                    "reason": "no visual reference and no scenarios",
                    "phase_status": {
                        "design": "completed",
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
        requirement_data = self.traceability.get_requirement(node_id) or requirement_data

        await self.log_cb(
            "InterfaceDesigner",
            f"Running unified design session for `{design_mode}`: understand requirement, explore codebase, materialize owned code, and return structured interfaces...",
            None,
            node_id,
        )
        design_bundle, design_messages = await self.interface_designer.design_bundle(
            node_id=node_id,
            requirement_data=requirement_data,
            design_mode=design_mode,
        )
        interfaces = design_bundle.get("interfaces", [])

        if not interfaces:
            role_label = "leaf" if is_leaf else "non-leaf"
            await self.log_cb(
                "InterfaceDesigner",
                f"Unified design session for the current {role_label} node did not return a valid interface JSON array.",
                "error",
                node_id,
            )
            return False

        self.traceability.clear_node_design_artifacts(node_id)
        if interfaces:
            self._store_interfaces(node_id, interfaces)

        materialized_files = extract_modified_files_from_messages(design_messages)
        if materialized_files:
            context_pipeline.cache.invalidate_file_layers(node_id)
            await self.log_cb(
                "InterfaceDesigner",
                f"Materialized files: {', '.join(sorted(materialized_files))}",
                None,
                node_id,
            )
        await self.log_cb(
            "InterfaceDesigner",
            f"Artifact summary: {json.dumps(self._build_design_summary_payload(interfaces, materialized_files), ensure_ascii=False)}",
            None,
            node_id,
        )

        self._update_node_session(
            node_id,
            {
                "interfaces": interfaces,
                "phase_status": {
                    "design": "completed",
                },
                "materialized_files": materialized_files,
            },
        )
        
        await self.log_cb(
            "InterfaceDesigner",
            f"Stored {len(interfaces)} interface definition(s) into traceability DB.",
            None,
            node_id,
        )

        # ---------------- Test generation ----------------
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
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
        self._update_node_session(
            node_id,
            {
                "execution_mode": "leaf_full" if is_leaf else "non_leaf_parent_shell",
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
            f"Artifact summary: {json.dumps(self._build_test_summary_payload(tests, modified_test_files), ensure_ascii=False)}",
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
        session = utils.load_node_session(node_id)
        if session.get("execution_mode") == "skipped_non_leaf":
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

        interfaces = self.traceability.list_interfaces(req_id=node_id)
        tests = self.traceability.list_tests(req_id=node_id)
        if not interfaces:
            await self.log_cb(
                "TestDrivenDeveloper",
                "IMPLEMENT phase requires designed interfaces, but none were found.",
                "warning",
                node_id,
            )

        if not tests:
            await self.log_cb(
                "TestDrivenDeveloper",
                "No generated tests were found for this node. IMPLEMENT cannot complete without generated test artifacts.",
                "warning",
                node_id,
            )

        final_ok = await self._run_tdd_batches_for_node(
            node_id=node_id,
            tests=tests,
        )
        
        for interface in interfaces:
            interface_id = str(interface.get("interface_id") or "").strip()
            if interface_id:
                self.traceability.set_interface_implemented(interface_id, True)
        
        if not final_ok:
            self._update_node_session(
                node_id,
                {
                    "phase_status": {"implement": "failed"},
                },
            )        
        else:
            self._update_node_session(
                node_id,
                {
                    "phase_status": {"implement": "completed"},
                },
            )
        
        return final_ok

    # ----- Persistence and execution helpers -----
    @staticmethod
    def _build_verification_passed(build_output: str) -> bool:
        exit_codes: list[int] = []
        for line in (build_output or "").splitlines():
            stripped = line.strip()
            if not stripped.startswith("Exit Code:"):
                continue
            try:
                exit_codes.append(int(stripped.split("Exit Code:", 1)[1].strip()))
            except ValueError:
                return False
        return bool(exit_codes) and all(code == 0 for code in exit_codes)

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
            existing = self.traceability.get_interface(interface_id)
            req_ids = merge_req_ids(existing, node_id)

            if reuse and existing:
                self.traceability.update_interface_fields(interface_id, req_ids=req_ids)
            else:
                self.traceability.upsert_interface(
                    interface_id=interface_id,
                    req_ids=req_ids,
                    type=str(interface.get("type", "")).strip(),
                    content=json.dumps(interface, ensure_ascii=False),
                    file_path=str(interface.get("file_path", "")).strip() or None,
                    first_line=str(interface.get("first_line", "")).strip() or None,
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

            self.traceability.upsert_test(
                test_id=test_id,
                req_id=node_id,
                interface_ids=normalize_string_list(test.get("interface_ids")),
                type=test_type,
                file_path=file_path or None,
                first_line=str(test.get("first_line", "")).strip() or None,
                passed=None,
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
