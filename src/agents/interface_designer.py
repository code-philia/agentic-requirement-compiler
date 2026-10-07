from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import Field

from agents.context.pipeline import context_pipeline
from agents.context.prompts.interface_designer import get_system_prompt
from agents.runtime.plain_codegen import (
    CodeEdits, Record, SharedNeed, SharedNeeded, DatabaseRepairNeeded, ModelTransportExhausted, apply_edits, ask_with_reads, feedback_source_paths, is_test_asset, protected_paths, source_bundle, shared_catalog,
)
from core import sessions
from core.service import get_runtime


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]
DESIGN_MAX_CALLS = 3


class DesignFiles(Record):
    frontend: list[str] = Field(default_factory=list, description="UI and frontend HTTP request files; frontend/src/api/*.ts belongs here, never API.")
    API: list[str] = Field(default_factory=list, description="Node-owned backend routes/controllers only; web paths must start with backend/.")
    FUNC: list[str] = Field(default_factory=list, description="Node-owned backend business service skeletons.")
    DB: list[str] = Field(default_factory=list, description="Node-owned backend database operation skeletons, not global schema/runtime.")
    shared: list[str] = Field(default_factory=list, description="Editable existing integration/registration files such as backend/src/app.js; not read-only shared core.")


class IdentityUsage(Record):
    required: bool
    reason: str = Field(min_length=1)


class InterfaceDesignResponse(CodeEdits):
    files: DesignFiles = Field(default_factory=DesignFiles)
    identity_usage: IdentityUsage | None = None


class InterfaceDesigner:
    """ReAct file design; the system owns scoped batch application."""

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
        feedback = ""
        requested_files: list[str] = []
        read_budget: dict[str, Any] = {}
        transport_budget: dict[str, int] = {}
        previous_candidate: dict[str, Any] | None = None
        for attempt in range(1, DESIGN_MAX_CALLS + 1):
            await self._log(f"Plain DESIGN call {attempt}/{DESIGN_MAX_CALLS}.", node_id=node_id)
            sessions.merge_node_session(node_id, {"design_codegen": {
                "call": attempt, "max_calls": DESIGN_MAX_CALLS, "status": "running"}})
            try:
                bundle = source_bundle(root, [path for path in required if path not in missing],
                                       frontend_roots=frontend_roots, design=True,
                                       feedback=feedback + "\n" + str(requested_files), node_id=node_id,
                                       frontend_only=bool(requirement_data.get("children_ids")), requirement=requirement_data)
                bundle["missing_tracked_files"] = missing
                edits = await ask_with_reads(self.model, get_system_prompt(), {
                    "model_stage": "DESIGN",
                    "node_id": node_id, "requirement": context_pipeline.task_requirement(node_id, requirement_data),
                    "declared_child_requirements": [
                        {key: child.get(key) for key in ("req_id", "name", "description")}
                        for child_id in requirement_data.get("children_ids") or []
                        if (child := get_runtime().traceability.get_requirement(child_id))],
                    "context": "\n\n".join([static, dynamic]),
                    **bundle, "protected_files": sorted(blocked), "feedback": feedback,
                    "previous_candidate": previous_candidate,
                }, InterfaceDesignResponse, root=root, budget=read_budget,
                    log=lambda message: self._log(message, node_id=node_id), transport_budget=transport_budget)
                if edits.shared_need:
                    apply_edits(root, edits, bundle["sources"], lambda path: False)
                if edits.identity_usage is None:
                    raise ValueError("Final DESIGN batch must include declare_identity_usage(required,reason), even when identity is not needed")
                identity = next((cap for cap in shared_catalog(root) if cap["name"] == "identity"), None)
                if edits.identity_usage.required and not (identity and identity.get("identity_contract")):
                    # No node files are applied before the shared protocol exists.
                    raise SharedNeeded(SharedNeed(name="identity", reason=edits.identity_usage.reason))
                previous_candidate = edits.model_dump()
                files = {layer: list(paths) for layer, paths in (session.get("file_groups") or {}).items()
                         if layer in {"frontend", "API", "FUNC", "DB", "shared"}}
                for layer, paths in edits.files.model_dump().items():
                    files.setdefault(layer, []).extend(path for path in paths if path not in files.get(layer, []))
                listed = {path for values in files.values() for path in values}
                deleted = {item.path for item in edits.delete_files}
                for layer in files:
                    files[layer] = [path for path in files[layer] if path not in deleted]
                listed -= deleted
                for item in [*edits.changes, *edits.new_files]:
                    if item.path in listed:
                        continue
                    path = item.path
                    if path.startswith("frontend/"):
                        layer = "frontend"
                    elif path.endswith('.sql') or Path(path).name == "package.json" or Path(path).stem in {"app", "main", "index", "server"}:
                        layer = "shared"
                    elif any(part in Path(path).parts for part in ("routes", "controllers", "api")):
                        layer = "API"
                    elif any(part in Path(path).parts for part in ("repositories", "dao", "db")):
                        layer = "DB"
                    elif any(part in Path(path).parts for part in ("services", "functions")):
                        layer = "FUNC"
                    else:
                        raise ValueError(f"edit_file for untracked file needs layer: {path}")
                    files[layer].append(path)
                    listed.add(path)
                edits.files = DesignFiles.model_validate(files)
                if listed & blocked or any(is_test_asset(path) for path in listed):
                    raise ValueError("DESIGN cannot claim protected backend/database files or test assets")
                unlisted = {item.path for item in [*edits.changes, *edits.new_files]} - listed
                if unlisted:
                    raise ValueError(
                        f"Modified/created files missing from files groups: {sorted(unlisted)}. "
                        "Declare every touched file. Existing application route-registration files "
                        "such as backend/src/app.js belong in files.shared if not protected. "
                        "Keep the API/FUNC/DB skeletons; repair the manifest rather than removing backend code.")
                if app_type == "web":
                    misplaced = [path for layer in ("API", "FUNC", "DB") for path in files[layer]
                                 if not path.startswith("backend/")]
                    if misplaced:
                        raise ValueError(
                            f"Backend groups API/FUNC/DB require backend/ paths: {misplaced}. "
                            "Move frontend HTTP request files into files.frontend. "
                            "Keep/create the corresponding backend routes, service and repository skeletons.")
                def allowed(path: str) -> bool:
                    return path in listed | deleted and path not in blocked and not is_test_asset(path)
                def deletable(path: str) -> bool:
                    return any(path.startswith(folder + "/") for folder in frontend_roots) and path not in blocked
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
                changed = apply_edits(root, edits, bundle["sources"], allowed, validate, deletable)
                from agents.runtime.plain_codegen import applied_batch_log
                await self._log(applied_batch_log(edits, changed), status="ok", node_id=node_id)
                sessions.merge_node_session(node_id, {"design_codegen": {
                    "identity_usage": edits.identity_usage.model_dump(),
                    "status": "accepted", "modified_files": changed, "feedback": "",
                    "accepted_edits": edits.model_dump(), "read_rounds": read_budget.get("rounds", 0),
                    "transport_retries": transport_budget.get("retries", 0)}})
                return {"files": files}
            except (SharedNeeded, DatabaseRepairNeeded):
                raise
            except ModelTransportExhausted as exc:
                sessions.merge_node_session(node_id, {"design_codegen": {
                    "status": "transport_failed", "feedback": str(exc),
                    "transport_retries": transport_budget.get("retries", 0)}})
                raise
            except Exception as exc:
                feedback = str(exc)[:8000]
                requested_files = list(dict.fromkeys([*requested_files, *feedback_source_paths(root, feedback)]))[:24]
                await self._log("flow> DESIGN batch rejected; repair follows:\n" + feedback, status="error", node_id=node_id)
                sessions.merge_node_session(node_id, {"design_codegen": {
                    "status": "rejected", "feedback": feedback, "requested_files": requested_files}})
        raise ValueError(f"DESIGN exhausted {DESIGN_MAX_CALLS} calls: {feedback}")

    async def _log(self, message: str, status: str | None = None, node_id: str | None = None) -> None:
        if self.log_cb:
            result = self.log_cb(self.agent_name, message, status, node_id)
            if inspect.isawaitable(result):
                await result
