from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from agents.context.pipeline import context_pipeline
from agents.context.prompts.test_driven_developer import get_system_prompt
from agents.runtime.implementation_scope import implementation_scope
from agents.runtime.plain_codegen import (
    CodeEdits, apply_edits, ask_with_reads, feedback_source_paths, protected_paths, source_bundle,
)
from core.service import get_runtime
from core import sessions


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


class TestDrivenDeveloper:
    """Ordinary implementation calls with a separate bounded source-reading budget."""

    agent_name = "TestDrivenDeveloper"

    def __init__(self, log_cb: LogCallback | None = None, *,
                 model: str | object | None = None, workspace_root: str | None = None,
                 requirement_path: str | None = None, app_type: str | None = None,
                 app_handler: Any | None = None) -> None:
        self.log_cb = log_cb
        self.model = model or os.environ.get("MODEL", "openai:gpt-5.4")
        self.workspace_root = workspace_root
        self.requirement_path = requirement_path or ""
        self.app_type = app_type
        self.app_handler = app_handler
        self.modified_files: list[str] = []
        self.read_budget: dict[str, Any] = {}

    async def run(self, *, node_id: str, test_files: list[str], test_type: str,
                  node_tests: list[dict[str, Any]] | None = None,
                  preloaded_source: str | None = None,
                  previous_failure_summary: str = "") -> str:
        self.modified_files = []
        root = Path(self.workspace_root or context_pipeline.config.workspace_dir
                    or os.environ.get("ARC_WORKSPACE_ROOT") or os.getcwd()).expanduser().resolve()
        app_type = (self.app_type or context_pipeline.config.app_type
                    or os.environ.get("ARC_APP_TYPE") or "web").strip().lower()
        runtime = get_runtime()
        records = runtime.traceability.list_interfaces()
        scope = implementation_scope(str(root), node_id, app_type, test_files, records)
        blocked = protected_paths(root, node_id, records)
        context_pipeline.configure(workspace_dir=str(root), app_type=app_type)
        static, dynamic = context_pipeline.build_agent_context_split(
            node_id=node_id, agent_type=self.agent_name, preloaded_source=preloaded_source,
            target_test_files=test_files)
        requested_files = feedback_source_paths(root, previous_failure_summary)
        sessions.merge_node_session(node_id, {"tdd_codegen": {"requested_files": requested_files}})
        required = [path for path in scope["allowed_files"] if (root / path).is_file()
                    or path not in scope["test_asset_files"] or path in test_files]
        bundle = source_bundle(root, required,
                               frontend_roots=scope["frontend_roots"],
                               feedback=previous_failure_summary + "\n" + str(requested_files), node_id=node_id,
                               frontend_wiring=app_type == "web")
        await self._log("Invoking plain implementation with bounded on-demand reads.", node_id=node_id)
        test_types = [str(test.get("type", "")) for test in node_tests or []] or [test_type]
        edits = await ask_with_reads(self.model, get_system_prompt(app_type, test_types), {
            "model_stage": "TDD",
            "node_id": node_id, "requirement": context_pipeline.task_requirement(node_id, runtime.traceability.get_requirement(node_id) or {}),
            "context": "\n\n".join([static, dynamic]), "implementation_scope": scope,
            "tests": node_tests or [], "test_type": test_type,
            "feedback": previous_failure_summary, "protected_files": sorted(blocked),
            **bundle,
        }, CodeEdits, root=root, budget=self.read_budget,
            log=lambda message: self._log(message, node_id=node_id))
        def allowed(path: str) -> bool:
            return path not in blocked and (
                path in scope["allowed_files"]
                or any(path.startswith(folder + "/") for folder in scope["frontend_roots"]))
        def deletable(path: str) -> bool:
            # Registered targets must survive for traceability and later repairs.
            return path not in scope["allowed_files"] and any(
                path.startswith(folder + "/") for folder in scope["frontend_roots"])
        self.modified_files = apply_edits(root, edits, bundle["sources"], allowed, deletable=deletable)
        deleted = {item.path for item in edits.delete_files}
        self.read_budget["files"] = [path for path in self.read_budget.get("files", []) if path not in deleted]
        sessions.merge_node_session(node_id, {"tdd_codegen": {
            "accepted_edits": edits.model_dump(), "read_rounds": self.read_budget.get("rounds", 0)}})
        await self._log(f"Applied {len(self.modified_files)} file(s); system validation follows.", node_id=node_id)
        return "APPLIED"

    async def _log(self, message: str, status: str | None = None, node_id: str | None = None) -> None:
        if self.log_cb:
            result = self.log_cb(self.agent_name, message, status, node_id)
            if inspect.isawaitable(result):
                await result
