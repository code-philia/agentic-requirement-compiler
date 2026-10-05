from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from app_type_handler import create_app_type_handler
from agents.context.pipeline import context_pipeline
from core import sessions
from core.service import get_runtime
from core.path_compat import normalize_windows_extended_prefix_text
from core.visual_analysis import analyze_and_attach_visual_references
from app_type_handler.test_results import parse_test_results
from agents.runtime.plain_codegen import SharedNeeded, feedback_source_paths


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]
TDD_MAX_CALLS = 4  # One initial generation plus at most three repair calls per invocation.
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
        self.interface_designer.validate_files = self._prepare_design_files

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
        try:
            interface_result = await self.interface_designer.run(
                node_id=node_id,
                requirement_data=requirement_data,
            )
            file_groups, prepared_interfaces = self._prepare_design_files(node_id, interface_result.get("files"), is_non_leaf)
        except ValueError as exc:
            self._update_node_session(node_id, {"phase_status": {"design": "failed"}})
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
        self, *, node_id: str, tests: list[dict[str, Any]],
    ) -> bool:
        """One generation call per round; validate every layer on current code."""
        groups: dict[str, list[dict[str, Any]]] = {}
        for test in tests:
            test_type = canonical_test_type(str(test.get("type", "")))
            if test_type is None:
                await self._log("TestDrivenDeveloper", "Unsupported registered test type.",
                                status="error", node_id=node_id)
                return False
            groups.setdefault(test_type, []).append(test)
        ordered = [kind for kind in TDD_BATCH_ORDER if kind in groups]
        if not ordered:
            return True
        feedback = str(sessions.load_node_session(node_id).get("recent_failure_summary") or "")
        final_ok = False
        modified: list[str] = []
        self.test_driven_developer.read_budget = {}
        for attempt in range(1, TDD_MAX_CALLS + 1):
            # Never reuse a passing layer from an earlier code revision.
            results: dict[str, str] = {}
            build_output = ""
            failures: list[str] = []
            await self._log("TestDrivenDeveloper", f"Plain TDD round {attempt}/{TDD_MAX_CALLS}.",
                            node_id=node_id)
            self._update_node_session(node_id, {"tdd_codegen": {
                "round": attempt, "max_calls": TDD_MAX_CALLS, "status": "running"}})
            try:
                await self.test_driven_developer.run(
                    node_id=node_id, test_files=collect_test_files(tests),
                    test_type="all", node_tests=tests, previous_failure_summary=feedback,
                )
                modified.extend(self.test_driven_developer.modified_files)
                context_pipeline.cache.invalidate_file_layers(node_id)
                build_output = await self.app_handler.run_build()
                await self._log("TestDrivenDeveloper", "Build output\n" + build_output,
                                status="debug", node_id=node_id)
                # Web returns one exit code per sub-build, rather than an aggregate.
                build_codes = re.findall(r"^\s*Exit Code:\s*(-?\d+)\s*$", build_output, re.MULTILINE)
                if not build_codes or any(int(code) != 0 for code in build_codes):
                    failures.append("Build: " + summarize_batch_output(build_output))
                else:
                    for kind in ordered:
                        output = await self.app_handler.run_test_group(kind, collect_test_files(groups[kind]))
                        results[kind] = output
                        await self._log("TestDrivenDeveloper", f"{kind} output\n{output}",
                                        status="debug", node_id=node_id)
                        if parse_test_results(output).get("exit_code") != 0:
                            failures.append(kind + ": " + summarize_batch_output(output))
            except SharedNeeded:
                raise
            except Exception as exc:
                failures.append("Generation/application/validation: " + str(exc)[:8000])
                await self._log("TestDrivenDeveloper", failures[-1], status="error", node_id=node_id)

            final_ok = not failures and len(results) == len(ordered)
            feedback = "\n\n".join(failures) or ("" if final_ok else "Validation did not complete.")
            for kind in ordered:
                passed = kind in results and parse_test_results(results[kind]).get("exit_code") == 0
                self.traceability.set_test_pass_statuses({
                    str(test["test_id"]): passed for test in groups[kind] if test.get("test_id")
                })
            self._update_node_session(node_id, {
                "recent_failure_summary": feedback,
                "tdd_codegen": {"status": "passed" if final_ok else "failed",
                                "build_output": build_output[-16000:],
                                "test_outputs": {kind: results.get(kind, "")[-16000:] for kind in ordered},
                                "modified_files": list(dict.fromkeys(modified))},
                "tdd_handoff": {"last_test_type": "all", "last_failed_output_summary": feedback,
                                "modified_files": list(dict.fromkeys(modified))},
            })
            context_pipeline.cache.invalidate_db_layers(node_id)
            context_pipeline.cache.invalidate_file_layers(node_id)
            if final_ok:
                break
        await self._log("TestDrivenDeveloper",
                        "Node passed build and all test layers." if final_ok else f"TDD exhausted {TDD_MAX_CALLS} calls.",
                        status="ok" if final_ok else "error", node_id=node_id)
        return final_ok

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
