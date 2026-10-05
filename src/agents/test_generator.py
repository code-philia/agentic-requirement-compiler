from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from pydantic import Field

from agents.context.pipeline import context_pipeline
from agents.context.prompts.test_generator import get_system_prompt
from agents.runtime.plain_codegen import (
    CodeEdits, Record, SharedNeeded, apply_edits, ask_with_reads, feedback_source_paths, is_test_asset,
    protected_paths, safe_path, source_bundle,
)
from core import sessions
from core.service import get_runtime


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]
TEST_GENERATION_MAX_CALLS = 3


def validate_relative_test_imports(root: Path, paths: set[str]) -> None:
    """Reject obvious local JS/TS path mistakes before handing tests to TDD."""
    for relative in sorted(paths):
        target = safe_path(root, relative)
        if target.suffix not in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"} or not target.is_file():
            continue
        source = target.read_text(encoding="utf-8")
        # Ignore comments; this is a conservative path check, not a module resolver.
        source = re.sub(r"/\*.*?\*/|^\s*//[^\n]*", "", source, flags=re.DOTALL | re.MULTILINE)
        modules = re.findall(r"(?:\bfrom\s*|\brequire\(\s*|\bimport\s*(?:\(\s*)?)['\"](\.[^'\"]+)['\"]", source)
        for module in modules:
            base = (target.parent / re.split(r"[?#]", module, maxsplit=1)[0]).resolve()
            if not base.is_relative_to(root):
                raise ValueError(f"Test import escapes workspace: {relative} -> {module}")
            candidates = [base]
            candidates += [Path(str(base) + ext) for ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json")]
            candidates += [base / ("index" + ext) for ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")]
            if base.suffix in {".js", ".jsx", ".mjs", ".cjs"}:
                candidates += [base.with_suffix(ext) for ext in (".ts", ".tsx", ".mts", ".cts")]
            if not any(path.is_file() for path in candidates) and not (base / "package.json").is_file():
                raise ValueError(f"Unresolved relative test import: {relative} -> {module}. Calculate the path from the test file directory; preserve the assertions.")


class TestManifestItem(Record):
    test_id: str = Field(min_length=1)
    type: Literal["Unit", "Integration", "E2E"]
    file_path: str = Field(min_length=1)


class TestGenerationResponse(CodeEdits):
    tests: list[TestManifestItem] = Field(default_factory=list)


class TestGenerator:
    """ReAct file generation scoped to tests and dependency manifests."""

    agent_name = "TestGenerator"

    def __init__(self, log_cb: LogCallback | None = None, *,
                 model: str | object | None = None, workspace_root: str | None = None,
                 requirement_path: str | None = None, app_type: str | None = None) -> None:
        self.log_cb = log_cb
        self.model = model or os.environ.get("MODEL", "openai:gpt-5.4")
        self.workspace_root = workspace_root
        self.requirement_path = requirement_path or ""
        self.app_type = app_type
        self.app_handler: Any | None = None
        self.validate_tests: Callable[..., Any] | None = None

    async def run(self, node_id: str, requirement_data: dict[str, Any], *,
                  preloaded_source: str | None = None, test_intent: str = "",
                  replace_test_id: str | None = None) -> tuple[list[dict[str, Any]] | None, str]:
        root = Path(self.workspace_root or context_pipeline.config.workspace_dir
                    or os.environ.get("ARC_WORKSPACE_ROOT") or os.getcwd()).expanduser().resolve()
        app_type = (self.app_type or context_pipeline.config.app_type
                    or os.environ.get("ARC_APP_TYPE") or "web").strip().lower()
        store = get_runtime().traceability
        all_tests = store.list_tests()
        existing = [test for test in all_tests if test.get("req_id") == node_id]
        by_id = {str(test["test_id"]): test for test in all_tests}
        selected = by_id.get(replace_test_id or "")
        blocked = protected_paths(root, node_id, store.list_interfaces())
        blocked.update(str(test.get("file_path") or "") for test in all_tests if test.get("req_id") != node_id)
        context_pipeline.configure(workspace_dir=str(root), app_type=app_type)
        static, dynamic = context_pipeline.build_agent_context_split(
            node_id=node_id, agent_type=self.agent_name, preloaded_source=preloaded_source)
        session = sessions.load_node_session(node_id)
        required = list(session.get("materialized_files") or [])
        required.extend(session.get("test_asset_files") or [])
        required.extend(test.get("file_path") for test in existing if test.get("file_path"))
        # Supply runner conventions/configuration and the real isolated DB harness.
        harness = ["package.json", "backend/package.json", "frontend/package.json",
                   "backend/vitest.config.js", "backend/vitest.config.ts", "frontend/vitest.config.ts",
                   "frontend/vitest.config.js", "backend/playwright.config.js",
                   "backend/playwright.config.ts", "backend/src/database/test_harness.js",
                   "playwright.config.ts", "playwright.config.js", "vitest.config.ts", "vitest.config.js",
                   "pyproject.toml", "app/__main__.py", "app/build.gradle", "build.gradle"]
        required.extend(path for path in harness if (root / path).is_file())
        required = list(dict.fromkeys(required))
        missing = [path for path in required if not (root / path).is_file()]
        frontend_roots = ["frontend"] if app_type == "web" else ["app/src/main"] if app_type == "android" else []
        test_roots = ["frontend/tests", "backend/tests", "backend/test-e2e"] if app_type == "web" else (
            ["app/src/test", "app/src/androidTest"] if app_type == "android" else ["tests"])
        feedback = ""
        requested_files: list[str] = []
        read_budget: dict[str, Any] = {}
        test_types = [str(selected.get("type", ""))] if selected else [str(test.get("type", "")) for test in existing]
        for attempt in range(1, TEST_GENERATION_MAX_CALLS + 1):
            await self._log(f"Plain test generation call {attempt}/{TEST_GENERATION_MAX_CALLS}.", node_id=node_id)
            sessions.merge_node_session(node_id, {"test_codegen": {
                "call": attempt, "max_calls": TEST_GENERATION_MAX_CALLS, "status": "running"}})
            try:
                bundle = source_bundle(root, [path for path in required if path not in missing],
                                       frontend_roots=frontend_roots, test_roots=test_roots,
                                       feedback=feedback + "\n" + str(requested_files), node_id=node_id,
                                       frontend_wiring=app_type == "web")
                bundle["missing_tracked_files"] = missing
                edits = await ask_with_reads(self.model, get_system_prompt(app_type, test_types), {
                    "model_stage": "TestGenerator",
                    "node_id": node_id, "requirement": context_pipeline.task_requirement(node_id, requirement_data),
                    "context": "\n\n".join([static, dynamic]), "existing_tests": existing,
                    "test_intent": test_intent, "replace_test_id": replace_test_id,
                    "protected_files": sorted(blocked), "feedback": feedback, **bundle,
                }, TestGenerationResponse, root=root, budget=read_budget,
                    log=lambda message: self._log(message, node_id=node_id))
                if edits.read_files or edits.shared_need:
                    apply_edits(root, edits, bundle["sources"], lambda path: False)
                tests = [test.model_dump() for test in edits.tests]
                test_types = [test["type"] for test in tests]
                ids = [test["test_id"] for test in tests]
                if len(set(ids)) != len(ids):
                    raise ValueError("Duplicate test_id in manifest")
                for test in tests:
                    prior = by_id.get(test["test_id"])
                    if not prior and not test["test_id"].startswith(node_id + ":"):
                        raise ValueError("New test IDs must start with node_id + ':'")
                    if prior and prior.get("req_id") != node_id:
                        raise ValueError("Test ID belongs to another requirement")
                    if prior and (prior.get("file_path") != test["file_path"] or prior.get("type") != test["type"]):
                        raise ValueError("Preserve existing test paths and types")
                if replace_test_id:
                    if not selected or selected.get("req_id") != node_id:
                        raise ValueError("Replacement requires a registered current-node test")
                    if ids != [replace_test_id]:
                        raise ValueError("Replacement manifest must contain only the selected test ID")
                    if tests[0]["file_path"] != selected.get("file_path"):
                        raise ValueError("Replacement must preserve the selected file path")
                elif test_intent and any(test_id in by_id for test_id in ids):
                    raise ValueError("Adding tests by intent must not overwrite existing IDs")
                paths = {test["file_path"] for test in tests}
                if paths & blocked or any(not is_test_asset(path) for path in paths):
                    raise ValueError("Test manifest claims protected/other-node files")
                if requirement_data.get("scenarios") and not test_intent and not any(test["type"] == "E2E" for test in tests):
                    raise ValueError("Declared scenarios require E2E coverage")
                if not tests and (edits.changes or edits.new_files or edits.delete_files):
                    raise ValueError("Empty test manifest must not change files")
                def allowed(path: str) -> bool:
                    if app_type == "web" and path in {"backend/package.json", "frontend/package.json"}:
                        return path not in blocked
                    if path in blocked or not is_test_asset(path):
                        return False
                    if not any(path.startswith(folder + "/") for folder in test_roots + ["frontend/test"]) and not self._is_test_config(path):
                        return False
                    if replace_test_id:
                        return path == selected.get("file_path")
                    # Added coverage cannot edit already registered tests, even if omitted from the manifest.
                    if test_intent and path in {test.get("file_path") for test in existing}:
                        return False
                    # Tests plus their helpers/configs; executable test files must be registered.
                    if path not in paths and self._is_executable_test(path):
                        return False
                    return True
                def validate() -> None:
                    if app_type == "web":
                        validate_relative_test_imports(root, paths | {item.path for item in edits.changes + edits.new_files})
                    for test in tests:
                        target = safe_path(root, test["file_path"])
                        if not target.is_file() or not target.read_text(encoding="utf-8").strip():
                            raise ValueError(f"Missing/empty test file: {test['file_path']}")
                        if self.app_handler:
                            error = self.app_handler.validate_test_path(test["type"], test["file_path"])
                            if error:
                                raise ValueError(error)
                    if self.validate_tests:
                        self.validate_tests(node_id=node_id, tests=tests)
                # Manifest checks participate in rollback and the same finite repair loop.
                def deletable(path: str) -> bool:
                    # Never remove registered tests/assets or runner config. Only
                    # obsolete unregistered helpers in the permitted test tree.
                    registered = {str(test.get("file_path", "")) for test in all_tests}
                    return not replace_test_id and path not in registered | set(required) | paths \
                        and not self._is_test_config(path) and not self._is_executable_test(path)
                changed = apply_edits(root, edits, bundle["sources"], allowed, validate, deletable)
                from agents.runtime.plain_codegen import applied_batch_log
                await self._log(applied_batch_log(edits, changed), status="ok", node_id=node_id)
                await self._log("flow> Test manifest accepted:\n" + "\n".join(
                    f"{test['type']}: {test['test_id']} -> {test['file_path']}" for test in tests),
                    status="ok", node_id=node_id)
                assets = list(dict.fromkeys([*(sessions.load_node_session(node_id).get("test_asset_files") or []),
                                             *paths, *(path for path in changed if is_test_asset(path))]))
                assets = [path for path in assets if (root / path).is_file()]
                payload = {"tests": tests, "files_written": [path for path in changed if (root / path).is_file()],
                           "files_deleted": [item.path for item in edits.delete_files]}
                sessions.merge_node_session(node_id, {"test_asset_files": assets, "test_codegen": {
                    "status": "accepted", "feedback": "", "modified_files": changed,
                    "accepted_edits": edits.model_dump(), "read_rounds": read_budget.get("rounds", 0)}})
                await self._log(f"Accepted {len(tests)} test artifact(s).", node_id=node_id)
                return tests, json.dumps(payload, ensure_ascii=False)
            except SharedNeeded:
                raise
            except Exception as exc:
                feedback = str(exc)[:8000]
                await self._log("flow> Test generation batch rejected; repair follows:\n" + feedback,
                                status="error", node_id=node_id)
                requested_files = list(dict.fromkeys([*requested_files, *feedback_source_paths(root, feedback)]))[:24]
                sessions.merge_node_session(node_id, {"test_codegen": {
                    "status": "rejected", "feedback": feedback, "requested_files": requested_files}})
                await self._log(feedback, status="error", node_id=node_id)
        sessions.merge_node_session(node_id, {"test_codegen": {"status": "failed"}})
        return None, feedback

    @staticmethod
    def _is_executable_test(path: str) -> bool:
        name = Path(path).name.lower()
        return ".test." in name or ".spec." in name or name.startswith("test_") or name.endswith("_test.py") or name.endswith("test.java")

    @staticmethod
    def _is_test_config(path: str) -> bool:
        return Path(path).name.lower().startswith(("vitest.config.", "playwright.config.", "jest.config.", "setup-tests.", "setuptests."))

    async def _log(self, message: str, status: str | None = None, node_id: str | None = None) -> None:
        if self.log_cb:
            result = self.log_cb(self.agent_name, message, status, node_id)
            if inspect.isawaitable(result):
                await result
