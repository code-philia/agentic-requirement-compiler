import json
import re
from collections import defaultdict
from typing import Any, Awaitable, Callable

import utils
from agents.context_pipeline import context_pipeline
from agents.tools.cli_tools import parse_test_results
from app_types import create_app_type_handler
from traceability.database import (
    clear_node_design_artifacts,
    get_interface_by_id,
    get_interfaces_by_req_id,
    get_tests_by_req_id,
    insert_call_edge,
    insert_interface,
    insert_test,
    reset_test_pass_statuses_for_req_id,
    update_interface_implemented_status,
    update_test_pass_statuses,
    update_interface_req_ids,
    upsert_implementation,
)
from utils import extract_json_array_from_markdown, extract_modified_files_from_messages

TEST_TYPE_ORDER = ["Unit", "Integration", "E2E"]
DEFAULT_TDD_TEST_BUDGET = 5


class WorkflowPhaseRunner:
    """Run the heavy DESIGN and IMPLEMENT phases while the workflow manager stays lightweight."""

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

    async def run_design_phase(self, node_id: str, requirement_data: dict[str, Any]) -> bool:
        is_leaf = not bool(requirement_data.get("children_ids"))

        await self.log_cb(
            "InterfaceDesigner",
            "Analyzing requirement context and designing interface IR...",
            None,
            node_id,
        )
        await self.interface_designer.parse_and_store_visual_elements(self.workspace_path, requirement_data)

        design_output, _ = await self.interface_designer.design_ir(
            node_id=node_id,
            requirement_data=requirement_data,
            is_leaf=is_leaf,
        )
        interfaces = extract_json_array_from_markdown(design_output)
        if not interfaces:
            await self.log_cb(
                "InterfaceDesigner",
                "DESIGN phase did not return a valid interface JSON array.",
                "error",
                node_id,
            )
            return False

        clear_node_design_artifacts(node_id)
        self._store_interfaces(node_id, interfaces)
        context_pipeline.cache.invalidate_db_layers(node_id)
        await self.log_cb(
            "InterfaceDesigner",
            f"Stored {len(interfaces)} interface definition(s) into traceability DB.",
            None,
            node_id,
        )

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
            test_type="All",
        )
        test_output, test_messages = await self.test_generator.run_from_messages(
            messages=messages,
            node_id=node_id,
            max_steps=20,
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

        self._store_tests(node_id, tests)
        context_pipeline.cache.invalidate_file_layers(node_id)

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
        interfaces = get_interfaces_by_req_id(node_id)
        tests = get_tests_by_req_id(node_id)
        if not interfaces:
            await self.log_cb(
                "TestDrivenDeveloper",
                "IMPLEMENT phase requires designed interfaces, but none were found.",
                "error",
                node_id,
            )
        if not tests:
            await self.log_cb(
                "TestDrivenDeveloper",
                "IMPLEMENT phase requires generated tests, but none were found.",
                "error",
                node_id,
            )

        total_model_test_runs = 0
        artifact_paths: set[str] = set()
        group_reports: list[str] = []

        for test_type in TEST_TYPE_ORDER:
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
                req_desc=requirement_data.get("description", ""),
                scenarios=requirement_data.get("scenarios", []),
                current_interfaces=interfaces,
            )
            implement_output, implement_messages = await self.test_driven_developer.run_from_messages(
                messages=messages,
                node_id=node_id,
                max_steps=25,
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

            total_model_test_runs += run_tests_usage.get("run_tests", 0)
            artifact_paths.update(extract_modified_files_from_messages(implement_messages))
            context_pipeline.cache.invalidate_file_layers(node_id)

            if "IMPLEMENTED" not in (implement_output or "").upper():
                await self.log_cb(
                    "TestDrivenDeveloper",
                    f"{test_type} group ended without explicit IMPLEMENTED. Continuing with system verification.",
                    None,
                    node_id,
                )

            group_passed, group_statuses = await self._execute_tests_for_records(
                node_id=node_id,
                test_type=test_type,
                tests=typed_tests,
                persist=False,
            )
            passed_count = sum(1 for value in group_statuses.values() if value is True)
            total_count = len(group_statuses)
            group_reports.append(
                f"{test_type}: {passed_count}/{total_count} passed after model used run_tests {run_tests_usage.get('run_tests', 0)}/{DEFAULT_TDD_TEST_BUDGET} times"
            )

            if group_passed:
                await self.log_cb(
                    "Compiler",
                    f"{test_type} group passed during interim verification.",
                    None,
                    node_id,
                )
            else:
                await self.log_cb(
                    "Compiler",
                    f"{test_type} group still has failing tests after budgeted TDD loop.",
                    "error",
                    node_id,
                )

        final_statuses = await self._finalize_test_results(node_id)
        total_tests = len(final_statuses)
        passed_tests = sum(1 for value in final_statuses.values() if value is True)
        failed_tests = sum(1 for value in final_statuses.values() if value is False)
        final_ok = total_tests > 0 and failed_tests == 0

        update_interface_implemented_status(node_id, final_ok)
        upsert_implementation(
            req_id=node_id,
            status="passed" if final_ok else "failed",
            attempts=total_model_test_runs,
            artifact_paths=sorted(artifact_paths),
            summary=self._build_implementation_summary(group_reports, passed_tests, total_tests),
        )

        if final_ok:
            await self.log_cb(
                "TestDrivenDeveloper",
                f"Final system test sweep passed: {passed_tests}/{total_tests}.",
                None,
                node_id,
            )
        else:
            await self.log_cb(
                "TestDrivenDeveloper",
                f"Final system test sweep failed: {passed_tests}/{total_tests} passed, {failed_tests} failed.",
                "error",
                node_id,
            )
        return final_ok

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

    def _store_tests(self, node_id: str, tests: list[dict[str, Any]]) -> None:
        for test in tests:
            if not isinstance(test, dict):
                continue

            test_id = str(test.get("test_id", "")).strip()
            if not test_id:
                continue

            insert_test(
                test_id=test_id,
                req_id=node_id,
                interface_ids=self._normalize_string_list(test.get("interface_ids")),
                type=str(test.get("type", "")).strip(),
                file_path=str(test.get("file_path", "")).strip(),
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

    async def _finalize_test_results(self, node_id: str) -> dict[str, bool | None]:
        tests = get_tests_by_req_id(node_id)
        reset_test_pass_statuses_for_req_id(node_id)

        status_by_test_id: dict[str, bool | None] = {}
        for test_type in TEST_TYPE_ORDER:
            typed_tests = [test for test in tests if str(test.get("type", "")).strip() == test_type]
            if not typed_tests:
                continue

            _, typed_statuses = await self._execute_tests_for_records(
                node_id=node_id,
                test_type=test_type,
                tests=typed_tests,
                persist=True,
            )
            status_by_test_id.update(typed_statuses)

        return status_by_test_id

    async def _execute_tests_for_records(
        self,
        node_id: str,
        test_type: str,
        tests: list[dict[str, Any]],
        persist: bool,
    ) -> tuple[bool, dict[str, bool | None]]:
        grouped_tests: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for test in tests:
            file_path = str(test.get("file_path", "")).strip()
            if file_path:
                grouped_tests[file_path].append(test)

        if not grouped_tests:
            await self.log_cb(
                "Compiler",
                f"No concrete {test_type} test files were found for execution.",
                "error",
                node_id,
            )
            return False, {}

        all_passed = True
        status_by_test_id: dict[str, bool | None] = {}
        phase_label = "final" if persist else "interim"

        for file_path, file_tests in grouped_tests.items():
            output = await self.app_handler.run_test_file(test_type, file_path)
            result = parse_test_results(output)
            file_passed = result.get("exit_code") == 0
            file_statuses = self._map_file_test_statuses(file_tests, result, file_passed)
            status_by_test_id.update(file_statuses)

            if persist:
                update_test_pass_statuses(file_statuses)

            if file_passed:
                await self.log_cb(
                    "Compiler",
                    f"{phase_label.capitalize()} {test_type} execution passed for {file_path}.",
                    None,
                    node_id,
                )
                continue

            all_passed = False
            await self.log_cb(
                "Compiler",
                f"{phase_label.capitalize()} {test_type} execution failed for {file_path}.",
                "error",
                node_id,
            )
            if utils.debug_logger:
                utils.debug_logger.log(
                    f"{phase_label.upper()}_TESTS[{node_id}:{test_type}:{file_path}]",
                    output,
                )

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

        outputs: list[str] = []
        exit_codes: list[int] = []
        for file_path in test_files:
            await self.log_cb(
                "Compiler",
                f"System is executing current {test_type} batch file: {file_path}",
                None,
                node_id,
            )
            output = await self.app_handler.run_test_file(test_type, file_path)
            parsed = parse_test_results(output)
            exit_codes.append(int(parsed.get("exit_code", -1)))
            outputs.append(f"=== Test File: {file_path} ===\n{output}")

        batch_exit_code = 0 if exit_codes and all(code == 0 for code in exit_codes) else 1
        header = [
            f"Exit Code: {batch_exit_code}",
            f"Batch Test Type: {test_type}",
            "Batch Test Files:",
        ]
        header.extend(f"- {file_path}" for file_path in test_files)
        header_text = "\n".join(header)
        return f"{header_text}\n\n" + "\n\n".join(outputs)

    def _map_file_test_statuses(
        self,
        file_tests: list[dict[str, Any]],
        parsed_result: dict[str, Any],
        file_passed: bool,
    ) -> dict[str, bool | None]:
        if file_passed:
            return {
                str(test.get("test_id", "")).strip(): True
                for test in file_tests
                if str(test.get("test_id", "")).strip()
            }

        normalized_passed = [self._normalize_test_text(item) for item in parsed_result.get("passed", [])]
        normalized_failed = [self._normalize_test_text(item) for item in parsed_result.get("failed", [])]

        status_by_test_id: dict[str, bool | None] = {}
        for test in file_tests:
            test_id = str(test.get("test_id", "")).strip()
            if not test_id:
                continue

            identifier = self._extract_test_identifier(test)
            normalized_identifier = self._normalize_test_text(identifier)
            if normalized_identifier:
                if any(normalized_identifier in item for item in normalized_passed):
                    status_by_test_id[test_id] = True
                    continue
                if any(normalized_identifier in item for item in normalized_failed):
                    status_by_test_id[test_id] = False
                    continue

            status_by_test_id[test_id] = False
        return status_by_test_id

    @staticmethod
    def _extract_test_identifier(test: dict[str, Any]) -> str:
        first_line = str(test.get("first_line", "")).strip()
        if not first_line:
            return str(test.get("test_id", "")).strip()

        quoted = re.search(r'["\']([^"\']+)["\']', first_line)
        if quoted:
            return quoted.group(1)

        method = re.search(r'([A-Za-z_][A-Za-z0-9_]*)\s*\(', first_line)
        if method:
            return method.group(1)

        return first_line

    @staticmethod
    def _normalize_test_text(value: str) -> str:
        return re.sub(r'[^a-z0-9]+', '', (value or '').lower())

    @staticmethod
    def _build_implementation_summary(group_reports: list[str], passed_tests: int, total_tests: int) -> str:
        summary_parts = list(group_reports)
        summary_parts.append(f"Final system sweep: {passed_tests}/{total_tests} tests passed.")
        return " | ".join(summary_parts)

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
