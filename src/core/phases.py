import os
import re
import json

from typing import Any, Awaitable, Callable
from tools.cli_tools import parse_test_results, run_build_impl

import core.utils as utils
from memory.context_pipeline import context_pipeline
from app_type_handler import create_app_type_handler
from core.service import get_runtime
from core.utils import extract_json_array_from_markdown, extract_modified_files_from_messages

TEST_TYPE_ORDER = ["Unit", "Integration", "E2E"]
DEFAULT_TDD_TEST_BUDGET = 10


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
        tests = self._normalize_registered_tests_for_execution(node_id, tests)
        previous_group_handoff_summary = initial_failure_summary.strip()
        previous_group_modified_files: list[str] = []

        for test_type in get_selected_test_types():
            typed_tests = [
                test
                for test in tests
                if normalize_test_type_name(
                    test.get("type", ""),
                    str(test.get("file_path", "")).strip(),
                    test.get("interface_ids"),
                ) == test_type
            ]
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
        owned_interface_types = sorted(derive_owned_interface_types(node_id, interfaces))
        
        await self.log_cb(
            "InterfaceDesigner",
            f"Stored {len(interfaces)} interface definition(s) into traceability DB.",
            None,
            node_id,
        )

        if not is_leaf:
            self._update_node_session(
                node_id,
                {
                    "execution_mode": "non_leaf_system_path",
                    "test_plan": build_test_plan([]),
                    "test_artifacts": [],
                    "test_policy": {
                        "enabled_layers": [],
                        "owned_interface_types": owned_interface_types,
                    },
                    "phase_status": {"test": "skipped"},
                },
            )
            context_pipeline.cache.invalidate_db_layers(node_id)
            await self.log_cb(
                "TestGenerator",
                "This parent-owned node keeps interface and composition artifacts only. Node-local test assets are not generated in this stage.",
                None,
                node_id,
            )
            return True

        # ---------------- Test generation ----------------
        enabled_test_types = select_leaf_test_types(node_id, interfaces)
        await self.log_cb(
            "TestGenerator",
            "Generating unit, integration, and end-to-end tests from the designed interfaces...",
            None,
            node_id,
        )
        tests, test_messages, _test_output = await self.test_generator.generate_tests_with_retry(
            node_id=node_id,
            requirement_data=requirement_data,
            design_mode=design_mode,
            enabled_test_types=enabled_test_types,
            validate=lambda artifacts: self._validate_generated_tests(
                node_id=node_id,
                tests=artifacts,
                is_leaf=is_leaf,
                allowed_test_types=enabled_test_types,
            ),
        )
        if not tests:
            await self.log_cb(
                "TestGenerator",
                (
                    "DESIGN phase test generation did not return a valid test JSON array "
                    f"after {self._structured_output_attempts()} attempt(s)."
                ),
                "error",
                node_id,
            )
            return False

        try:
            self._store_tests(
                node_id,
                tests,
                is_leaf=is_leaf,
                allowed_test_types=enabled_test_types,
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
                "execution_mode": "leaf_full",
                "test_plan": build_test_plan(tests),
                "test_artifacts": tests,
                "test_policy": {
                    "enabled_layers": enabled_test_types,
                    "owned_interface_types": owned_interface_types,
                },
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
        if session.get("execution_mode") == "non_leaf_system_path":
            final_ok = await self._run_non_leaf_system_path_check(
                node_id=node_id,
                interfaces=interfaces,
            )

            for interface in interfaces:
                interface_id = str(interface.get("interface_id") or "").strip()
                if interface_id:
                    self.traceability.set_interface_implemented(interface_id, True)

            self._update_node_session(
                node_id,
                {
                    "phase_status": {"implement": "completed" if final_ok else "failed"},
                },
            )
            return final_ok

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

        current_node_interfaces = session.get("interfaces") or interfaces
        final_ok = await self._run_tdd_batches_for_node(
            node_id=node_id,
            tests=tests,
            scope_note=build_leaf_scope_note(node_id, current_node_interfaces),
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

    def _normalize_registered_tests_for_execution(
        self,
        node_id: str,
        tests: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized_tests: list[dict[str, Any]] = []
        for test in tests:
            if not isinstance(test, dict):
                continue
            normalized_type = normalize_test_type_name(
                test.get("type", ""),
                str(test.get("file_path", "")).strip(),
                test.get("interface_ids"),
            )
            if not normalized_type:
                normalized_tests.append(test)
                continue
            if normalized_type != str(test.get("type", "")).strip():
                test_id = str(test.get("test_id", "")).strip()
                if test_id:
                    self.traceability.update_test_fields(test_id, type=normalized_type)
                test = {
                    **test,
                    "type": normalized_type,
                }
            normalized_tests.append(test)
        return normalized_tests

    async def _run_non_leaf_system_path_check(
        self,
        node_id: str,
        interfaces: list[dict[str, Any]],
    ) -> bool:
        await self.log_cb(
            "TestDrivenDeveloper",
            "Verifying the parent-owned system path by checking that the current composition remains buildable.",
            None,
            node_id,
        )
        build_output = await run_build_impl()
        passed = self._build_verification_passed(build_output)
        failure_summary = "" if passed else summarize_batch_output(build_output)
        self._update_node_session(
            node_id,
            {
                "tdd_handoff": {
                    "last_test_type": "SystemPath",
                    "last_failed_output_summary": failure_summary,
                    "modified_files": [],
                    "root_cause_notes": (
                        ["System path check passed."]
                        if passed else
                        ["System path check failed and requires interface or composition repair."]
                    ),
                },
                "recent_failure_summary": failure_summary,
            },
        )
        await self.log_cb(
            "Compiler",
            "System path check passed." if passed else "System path check failed.",
            None if passed else "error",
            node_id,
        )
        return passed

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

    def _structured_output_attempts(self) -> int:
        from core.structured_output import STRUCTURED_OUTPUT_RETRY_COUNT

        return STRUCTURED_OUTPUT_RETRY_COUNT + 1

    def _validate_generated_tests(
        self,
        node_id: str,
        tests: list[dict[str, Any]],
        is_leaf: bool,
        allowed_test_types: list[str],
    ) -> str | None:
        generated_ids: set[str] = set()
        sequence = 0

        for test in tests:
            if not isinstance(test, dict):
                continue

            raw_test_id = str(test.get("test_id", "")).strip()
            if not raw_test_id:
                continue

            file_path = str(test.get("file_path", "")).strip()
            test_type = normalize_test_type_name(
                test.get("type", ""),
                file_path,
                test.get("interface_ids"),
            )
            if not test_type:
                return f"Generated test `{raw_test_id}` is missing `type`."
            if not file_path:
                return f"Generated test `{raw_test_id}` is missing `file_path`."

            if not is_leaf:
                return (
                    f"Generated test `{raw_test_id}` is invalid for non-leaf node `{node_id}`. "
                    "Non-leaf nodes should not register tests."
                )

            if test_type not in allowed_test_types:
                return (
                    f"Generated test `{raw_test_id}` resolved to unsupported layer `{test_type}` for node `{node_id}`. "
                    f"Allowed layers: {', '.join(allowed_test_types) if allowed_test_types else 'none'}."
                )

            validation_error = self.app_handler.validate_test_path(test_type, file_path)
            if validation_error:
                return f"Generated test `{raw_test_id}` has an invalid path. {validation_error}"

            sequence += 1
            test_id = canonicalize_test_id(
                node_id=node_id,
                test_type=test_type,
                raw_test_id=raw_test_id,
                sequence=sequence,
            )
            if test_id in generated_ids:
                return (
                    f"Generated duplicate canonical test id `{test_id}` for node `{node_id}`. "
                    "Each stored test must be globally unique."
                )
            generated_ids.add(test_id)

        if sequence == 0:
            return "Test JSON array did not contain any valid test entries with non-empty `test_id`."
        return None

    def _store_tests(
        self,
        node_id: str,
        tests: list[dict[str, Any]],
        is_leaf: bool,
        allowed_test_types: list[str],
    ) -> None:
        generated_ids: set[str] = set()
        sequence = 0

        for test in tests:
            if not isinstance(test, dict):
                continue

            raw_test_id = str(test.get("test_id", "")).strip()
            if not raw_test_id:
                continue

            file_path = str(test.get("file_path", "")).strip()
            test_type = normalize_test_type_name(
                test.get("type", ""),
                file_path,
                test.get("interface_ids"),
            )
            if not is_leaf:
                raise ValueError(
                    f"Generated test `{raw_test_id}` is invalid for non-leaf node `{node_id}`. "
                    "Non-leaf nodes should not register tests; they only keep shared interface and aggregation artifacts."
                )
            if test_type not in allowed_test_types:
                raise ValueError(
                    f"Generated test `{raw_test_id}` resolved to unsupported layer `{test_type}` for node `{node_id}`. "
                    f"Allowed layers: {', '.join(allowed_test_types) if allowed_test_types else 'none'}."
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




def has_visual_reference_hint(requirement_data: dict[str, Any]) -> bool:
    visual_reference = requirement_data.get("visual_reference") or []
    if visual_reference:
        return True
    description = str(requirement_data.get("description", "") or "")
    return bool(re.search(r"!\[[^\]]*\]\(([^)]+)\)", description))


def classify_non_leaf_work(requirement_data: dict[str, Any]) -> str:
    scenarios = requirement_data.get("scenarios") or []
    if scenarios:
        return "non_leaf_full"
    if has_visual_reference_hint(requirement_data):
        return "non_leaf_ui_only"
    return "skip"


def build_base_node_session(
    node_id: str,
    requirement_data: dict[str, Any],
    design_mode: str,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "design_mode": design_mode,
        "phase_status": {
            "design": "pending",
            "test": "pending",
            "implement": "pending",
        },
        "requirement_snapshot": {
            "name": requirement_data.get("name", ""),
            "description": requirement_data.get("description", ""),
            "children_ids": requirement_data.get("children_ids") or [],
            "dependencies": requirement_data.get("dependencies") or [],
        },
        "recent_failure_summary": "",
    }


def derive_owned_interface_types(
    node_id: str,
    interfaces: list[dict[str, Any]],
) -> set[str]:
    owned_types: set[str] = set()
    prefix = f"{str(node_id or '').strip()}-"
    for interface in interfaces or []:
        if not isinstance(interface, dict):
            continue
        interface_id = str(interface.get("interface_id", "")).strip()
        if not interface_id.startswith(prefix):
            continue
        interface_type = str(interface.get("type", "")).strip().upper()
        if interface_type:
            owned_types.add(interface_type)
    return owned_types


def select_leaf_test_types(
    node_id: str,
    interfaces: list[dict[str, Any]],
) -> list[str]:
    owned_types = derive_owned_interface_types(node_id, interfaces)
    enabled_layers: list[str] = []
    if any(interface_type in {"FUNC", "DB"} for interface_type in owned_types):
        enabled_layers.append("Unit")
    if owned_types:
        enabled_layers.append("Integration")
    if "UI" in owned_types:
        enabled_layers.append("E2E")
    if not enabled_layers:
        enabled_layers.append("Integration")
    return enabled_layers


def build_leaf_scope_note(
    node_id: str,
    interfaces: list[dict[str, Any]],
) -> str:
    owned_types = sorted(derive_owned_interface_types(node_id, interfaces))
    if not owned_types:
        return (
            "Treat this node as a thin boundary closure. Keep repairs on the smallest reused owner file that the "
            "current failing batch proves necessary."
        )
    if set(owned_types).issubset({"UI"}):
        return (
            "Current node ownership is UI-only. Keep tests and repairs in the rendered form and the immediate frontend "
            "request boundary. Reused backend or DB collaborators are fallback dependencies and should change only when "
            "the current failing batch proves a reused route or persistence mismatch."
        )
    if set(owned_types).issubset({"UI", "API"}):
        return (
            "Current node ownership is limited to frontend UI and request wiring. Prefer fixes in the page, route "
            "container, or frontend API helper before changing reused backend services."
        )
    if "UI" not in owned_types:
        return (
            "Current node does not own a browser surface. Keep repairs in backend or service boundaries and avoid "
            "inventing page-level behavior."
        )
    return (
        "Current node owns an executable feature chain. Start at the smallest failing owner layer and expand outward "
        "only when direct evidence shows the boundary is reused and broken upstream."
    )


def normalize_test_type_name(
    test_type: Any,
    file_path: str,
    interface_ids: Any = None,
) -> str:
    raw_type = str(test_type or "").strip()
    normalized_raw = raw_type.lower()
    direct_map = {
        "unit": "Unit",
        "integration": "Integration",
        "e2e": "E2E",
    }
    if normalized_raw in direct_map:
        return direct_map[normalized_raw]

    normalized_path = str(file_path or "").strip().replace("\\", "/").lower()
    interface_type_hints: set[str] = set()
    for interface_id in normalize_string_list(interface_ids):
        match = re.search(r"-(UI|API|FUNC|DB)-", str(interface_id).strip(), re.IGNORECASE)
        if match:
            interface_type_hints.add(match.group(1).upper())

    candidate_types: list[str] = []
    for separator in ("|", "/", ","):
        if separator in raw_type:
            candidate_types = [
                direct_map.get(part.strip().lower(), "")
                for part in raw_type.split(separator)
                if part.strip()
            ]
            break
    candidate_types = [candidate for candidate in candidate_types if candidate]
    if not candidate_types:
        if "/test-e2e/" in normalized_path:
            return "E2E"
        if normalized_path.startswith("frontend/tests/") or normalized_path.startswith("backend/tests/"):
            return "Integration"
        return ""

    if len(candidate_types) == 1:
        return candidate_types[0]
    if "/test-e2e/" in normalized_path:
        return "E2E"
    if interface_type_hints & {"UI", "API"}:
        return "Integration"
    if interface_type_hints and interface_type_hints <= {"FUNC", "DB"}:
        return "Unit"
    if normalized_path.startswith("frontend/tests/"):
        return "Integration"
    if "Integration" in candidate_types:
        return "Integration"
    return candidate_types[0]


def build_test_plan(tests: list[dict[str, Any]]) -> dict[str, Any]:
    grouped = {
        "unit_files": [],
        "integration_files": [],
        "e2e_files": [],
        "test_ids": [],
    }
    for test in tests:
        if not isinstance(test, dict):
            continue
        file_path = str(test.get("file_path", "")).strip()
        test_type = normalize_test_type_name(
            test.get("type", ""),
            file_path,
            test.get("interface_ids"),
        )
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


def summarize_batch_output(batch_output: str, max_lines: int = 30) -> str:
    lines = [line for line in (batch_output or "").splitlines() if line.strip()]
    if not lines:
        return ""
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        lines.insert(0, "...[truncated]")
    return "\n".join(lines)


def get_selected_test_types() -> list[str]:
    return list(TEST_TYPE_ORDER)


def canonicalize_test_id(
    node_id: str,
    test_type: str,
    raw_test_id: str,
    sequence: int,
) -> str:
    sanitized_node_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(node_id or "").strip()) or "NODE"
    sanitized_type = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(test_type or "").strip()) or "TEST"
    sanitized_raw = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(raw_test_id or "").strip()) or "TEST"
    return f"{sanitized_node_id}::{sanitized_type}::{sequence:03d}::{sanitized_raw}"


def build_group_handoff_summary(
    node_id: str,
    test_type: str,
    modified_files: list[str],
    group_statuses: dict[str, bool | None],
) -> str:
    passed_tests = sorted(test_id for test_id, status in group_statuses.items() if status is True)
    failed_tests = sorted(test_id for test_id, status in group_statuses.items() if status is not True)
    summary_lines = [
        f"- Previous group: {test_type}",
        f"- Modified files: {', '.join(sorted(modified_files)) if modified_files else 'none'}",
        f"- Tests passed in previous group: {', '.join(passed_tests[:12]) if passed_tests else 'none'}",
        f"- Remaining failing tests from previous group: {', '.join(failed_tests[:12]) if failed_tests else 'none'}",
    ]
    return "\n".join(summary_lines)


def map_statuses_from_batch_output(
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
    file_batch_statuses = extract_file_batch_statuses(parsed_result)
    status_by_test_id: dict[str, bool | None] = {}
    all_passed = True

    for file_path, file_tests in grouped_tests.items():
        normalized_file_path = file_path.replace("\\", "/")
        file_passed = file_batch_statuses.get(normalized_file_path, batch_passed)
        file_statuses = _map_file_test_statuses(file_tests, file_passed)
        status_by_test_id.update(file_statuses)
        if not all(value is True for value in file_statuses.values()):
            all_passed = False

    return all_passed, status_by_test_id

def extract_file_batch_statuses(parsed_result: dict[str, Any]) -> dict[str, bool]:
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


def group_sub_batches_by_requested_file(parsed_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
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


def collect_test_files(tests: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for test in tests:
        file_path = str(test.get("file_path", "")).strip()
        if file_path and file_path not in seen:
            seen.append(file_path)
    return seen


def merge_req_ids(existing: dict[str, Any] | None, node_id: str) -> list[str]:
    req_ids = list(existing.get("req_ids", [])) if existing else []
    if node_id not in req_ids:
        req_ids.append(node_id)
    return req_ids


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _map_file_test_statuses(
    file_tests: list[dict[str, Any]],
    file_passed: bool,
) -> dict[str, bool | None]:
    return {
        str(test.get("test_id", "")).strip(): file_passed
        for test in file_tests
        if str(test.get("test_id", "")).strip()
    }
