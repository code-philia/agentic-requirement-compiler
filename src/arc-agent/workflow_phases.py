import json
from collections import defaultdict
from typing import Any, Awaitable, Callable

import utils
from agents.context_pipeline import context_pipeline
from agents.tools.cli_tools import parse_test_results, run_tests_impl
from traceability.database import (
    clear_node_design_artifacts,
    get_interface_by_id,
    get_interfaces_by_req_id,
    get_tests_by_req_id,
    insert_call_edge,
    insert_interface,
    insert_test,
    update_interface_implemented_status,
    update_interface_req_ids,
    update_test_implemented_status,
    upsert_implementation,
)
from traceability.test_result_tracker import TestResultTracker
from utils import extract_json_array_from_markdown, extract_modified_files_from_messages

TEST_TYPE_ORDER = ["Unit", "Integration", "E2E"]


class WorkflowPhaseRunner:
    """Run the heavy DESIGN and IMPLEMENT phases while the workflow manager stays lightweight."""

    def __init__(
        self,
        workspace_path: str,
        arc_dir: str,
        interface_designer,
        test_generator,
        test_driven_developer,
        log_cb: Callable[[str, str, str | None, str | None], Awaitable[None] | None],
    ):
        self.workspace_path = workspace_path
        self.arc_dir = arc_dir
        self.interface_designer = interface_designer
        self.test_generator = test_generator
        self.test_driven_developer = test_driven_developer
        self.log_cb = log_cb

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
            return False
        if not tests:
            await self.log_cb(
                "TestDrivenDeveloper",
                "IMPLEMENT phase requires generated tests, but none were found.",
                "error",
                node_id,
            )
            return False

        attempts = 0
        artifact_paths: set[str] = set()

        for test_type in TEST_TYPE_ORDER:
            typed_tests = [test for test in tests if str(test.get("type", "")).strip() == test_type]
            if not typed_tests:
                continue

            attempts += 1
            test_files = self._collect_test_files(typed_tests)

            await self.log_cb(
                "TestDrivenDeveloper",
                f"Implementing against {test_type} tests in {len(test_files)} file(s)...",
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
            )

            artifact_paths.update(extract_modified_files_from_messages(implement_messages))
            context_pipeline.cache.invalidate_file_layers(node_id)

            if "IMPLEMENTED" not in (implement_output or "").upper():
                await self.log_cb(
                    "TestDrivenDeveloper",
                    "Agent did not explicitly return IMPLEMENTED. Running explicit test verification anyway.",
                    None,
                    node_id,
                )

            verified = await self._verify_test_group(node_id, test_type, typed_tests)
            if not verified:
                update_interface_implemented_status(node_id, False)
                upsert_implementation(
                    req_id=node_id,
                    status="failed",
                    attempts=attempts,
                    artifact_paths=sorted(artifact_paths),
                    summary=f"{test_type} verification failed.",
                )
                return False

        if attempts == 0:
            await self.log_cb(
                "TestDrivenDeveloper",
                "IMPLEMENT phase found no runnable test groups.",
                "error",
                node_id,
            )
            return False

        update_interface_implemented_status(node_id, True)
        upsert_implementation(
            req_id=node_id,
            status="passed",
            attempts=attempts,
            artifact_paths=sorted(artifact_paths),
            summary="All generated tests passed during IMPLEMENT phase.",
        )
        await self.log_cb(
            "TestDrivenDeveloper",
            "All generated tests passed. Implementation trace updated.",
            None,
            node_id,
        )
        return True

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

    async def _verify_test_group(self, node_id: str, test_type: str, tests: list[dict[str, Any]]) -> bool:
        tracker = TestResultTracker(self.arc_dir)
        grouped_tests: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for test in tests:
            file_path = str(test.get("file_path", "")).strip()
            if not file_path:
                continue
            grouped_tests[file_path].append(test)

        if not grouped_tests:
            await self.log_cb(
                "Compiler",
                f"No concrete {test_type} test files were found for verification.",
                "error",
                node_id,
            )
            return False

        all_passed = True
        for file_path, file_tests in grouped_tests.items():
            output = await run_tests_impl(test_type=test_type, test_file_path=file_path)
            result = parse_test_results(output)
            passed = result.get("exit_code") == 0
            status = "direct_pass" if passed else "final_fail"
            test_ids: list[str] = []

            for test in file_tests:
                test_id = str(test.get("test_id", "")).strip()
                if not test_id:
                    continue
                test_ids.append(test_id)
                tracker.record_test(
                    node_id=node_id,
                    test_type=test_type,
                    test_id=test_id,
                    file_path=file_path,
                    status=status,
                    attempts=1,
                )

            update_test_implemented_status(test_ids, implemented=passed)

            if passed:
                await self.log_cb(
                    "Compiler",
                    f"Verified {test_type} test file: {file_path}",
                    None,
                    node_id,
                )
                continue

            all_passed = False
            await self.log_cb(
                "Compiler",
                f"{test_type} verification failed for {file_path}. See debug.log for details.",
                "error",
                node_id,
            )
            if utils.debug_logger:
                utils.debug_logger.log(
                    f"VERIFY_TESTS[{node_id}:{test_type}:{file_path}]",
                    output,
                )

        return all_passed

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
