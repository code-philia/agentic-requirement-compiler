from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import Field

from agents.context.pipeline import context_pipeline
from agents.context.prompts.interface_designer import get_system_prompt
from agents.runtime.plain_codegen import (
    CodeEdits, Record, apply_edits, ask_for_edits, is_test_asset, protected_paths, source_bundle,
)
from core import sessions
from core.service import get_runtime


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]
DESIGN_MAX_CALLS = 3


class DesignFiles(Record):
    frontend: list[str] = Field(default_factory=list)
    API: list[str] = Field(default_factory=list)
    FUNC: list[str] = Field(default_factory=list)
    DB: list[str] = Field(default_factory=list)
    shared: list[str] = Field(default_factory=list)


class InterfaceDesignResponse(CodeEdits):
    files: DesignFiles


class InterfaceDesigner:
    """Bounded plain-LLM design; the system owns file application."""

    agent_name = "InterfaceDesigner"

    def __init__(self, log_cb: LogCallback | None = None, *,
                 model: str | object | None = None, workspace_root: str | None = None,
                 requirement_path: str | None = None, app_type: str | None = None) -> None:
        self.log_cb = log_cb
        self.model = model or os.environ.get("MODEL", "openai:gpt-5.4")
        self.workspace_root = workspace_root
        self.requirement_path = requirement_path or ""
        self.app_type = app_type
        # Installed by the workflow to reuse its authoritative manifest validator.
        self.validate_files: Callable[..., Any] | None = None

    async def run(self, *, node_id: str, requirement_data: dict[str, Any]) -> dict[str, Any]:
        root = Path(self.workspace_root or context_pipeline.config.workspace_dir
                    or os.environ.get("ARC_WORKSPACE_ROOT") or os.getcwd()).expanduser().resolve()
        app_type = (self.app_type or context_pipeline.config.app_type
                    or os.environ.get("ARC_APP_TYPE") or "web").strip().lower()
        context_pipeline.configure(workspace_dir=str(root), app_type=app_type)
        static, dynamic = context_pipeline.build_agent_context_split(
            node_id=node_id, agent_type=self.agent_name)
        records = get_runtime().traceability.list_interfaces()
        blocked = protected_paths(root, node_id, records)
        session = sessions.load_node_session(node_id)
        required = list(session.get("materialized_files") or [])
        # Parent/dependency file mappings are relevant inputs, not backend edit targets.
        related = list(requirement_data.get("dependencies") or [])
        parent_id = str(requirement_data.get("parent_id") or "")
        seen = {node_id}
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            related.append(parent_id)
            parent_id = str((get_runtime().traceability.get_requirement(parent_id) or {}).get("parent_id") or "")
        for related_id in related:
            if isinstance(related_id, str):
                required.extend(sessions.load_node_session(related_id).get("materialized_files") or [])
        frontend_roots = ["frontend"] if app_type == "web" else ["app/src/main"] if app_type == "android" else []
        required = list(dict.fromkeys(required))
        missing = [path for path in required if not (root / path).is_file()]
        bundle = source_bundle(root, [path for path in required if path not in missing],
                               frontend_roots=frontend_roots, design=True)
        bundle["missing_tracked_files"] = missing
        feedback = ""
        for attempt in range(1, DESIGN_MAX_CALLS + 1):
            await self._log(f"Plain DESIGN call {attempt}/{DESIGN_MAX_CALLS}.", node_id=node_id)
            sessions.merge_node_session(node_id, {"design_codegen": {
                "call": attempt, "max_calls": DESIGN_MAX_CALLS, "status": "running"}})
            try:
                edits = await ask_for_edits(self.model, get_system_prompt(), {
                    "node_id": node_id, "requirement": requirement_data,
                    "context": "\n\n".join([static, dynamic]),
                    **bundle, "protected_files": sorted(blocked), "feedback": feedback,
                }, InterfaceDesignResponse)
                files = edits.files.model_dump()
                listed = {path for values in files.values() for path in values}
                if listed & blocked or any(is_test_asset(path) for path in listed):
                    raise ValueError("DESIGN cannot claim protected backend/database files or test assets")
                def allowed(path: str) -> bool:
                    return path in listed and path not in blocked and not is_test_asset(path)
                def validate() -> None:
                    if requirement_data.get("children_ids") and any(files[layer] for layer in ("API", "FUNC", "DB")):
                        raise ValueError("Parent DESIGN cannot own backend files")
                    if self.validate_files:
                        self.validate_files(node_id, files, bool(requirement_data.get("children_ids")))
                    else:
                        from agents.runtime.plain_codegen import safe_path
                        for path in listed:
                            if not safe_path(root, path).is_file():
                                raise ValueError(f"Missing DESIGN file: {path}")
                changed = apply_edits(root, edits, bundle["sources"], allowed, validate)
                sessions.merge_node_session(node_id, {"design_codegen": {
                    "status": "accepted", "modified_files": changed, "feedback": "",
                    "accepted_edits": edits.model_dump()}})
                return {"files": files}
            except Exception as exc:
                feedback = str(exc)[:8000]
                await self._log(feedback, status="error", node_id=node_id)
                sessions.merge_node_session(node_id, {"design_codegen": {
                    "status": "rejected", "feedback": feedback}})
        raise ValueError(f"DESIGN exhausted {DESIGN_MAX_CALLS} calls: {feedback}")

    async def _log(self, message: str, status: str | None = None, node_id: str | None = None) -> None:
        if self.log_cb:
            result = self.log_cb(self.agent_name, message, status, node_id)
            if inspect.isawaitable(result):
                await result
