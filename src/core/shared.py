"""Discover and implement one shared capability only when a node requests it."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import Field

from agents.runtime.plain_codegen import (
    CodeEdits, Record, SharedNeed, apply_edits, ask_with_reads, feedback_source_paths,
    is_test_asset, protected_paths, safe_path, source_bundle, shared_index,
)
from core.files import read_json_file, write_json_file


class Capability(Record):
    name: str = Field(min_length=1)
    files: list[str] = Field(default_factory=list)
    reuse_files: list[str] = Field(default_factory=list)
    contract: str = Field(min_length=1)


class Discovery(Record):
    capability: Capability | None = None
    read_files: list[str] = Field(default_factory=list)
    read_shared: list[str] = Field(default_factory=list)
    read_shared_groups: list[str] = Field(default_factory=list)
    database_gap: str = ""


DISCOVERY_PROMPT = """Resolve only the shared_need raised by the current requirement.
Do not analyze the whole requirement tree or design future shared infrastructure.
Return JSON matching response_schema. First inspect the catalog and supplied source:
reuse actual existing exports rather than create a parallel implementation.
Return one capability with stable files and a short source contract, or read_files
with no capability to request missing evidence. Use database_gap only when required
persistence cannot be implemented on the prepared schema; never add schema or seeds.
Owned files belong under backend/src/shared or frontend/src/shared for web,
app/shared for CLI, or app/src/main/java/<package>/shared for Android.
reuse_files are existing read-only infrastructure, not feature-owned business modules.
Reuse db_runtime.js/withTransaction if available. Do not relocate existing code.
Specify exported names, inputs/results/errors and the lifecycle/identity/connection
rules needed by this consumer. Keep feature workflows in the node's own modules.
An existing capability is immutable: consume it or register a separate small adapter,
never rename it or silently change its contract to satisfy one consumer.
"""


IMPLEMENT_PROMPT = """Implement only this newly requested shared capability.
Existing catalog modules and reuse_files are read-only; preserve their contracts.
Implement real behavior using the prepared database, not skeletons or fake success.
Do not add database structure/seeds or a parallel database/session/transaction runtime.
Use installed dependencies. Preserve credentials, identity, session lifetime and
transaction connection semantics. Node business workflows remain node-owned.
Only declared shared files may be created; existing integration files may be edited
around compiler bootstrap hooks. Do not edit feature-owned modules or test assets.
If evidence is missing, return read_files only. Never emit another shared_need here.
The system builds and provides bounded repair feedback. Return code edits only.
"""


class SharedPreparation:
    def __init__(self, workspace_path: str, app_type: str, log_cb):
        self.root = Path(workspace_path).resolve()
        self.app_type = app_type
        self.log_cb = log_cb
        self.state_path = self.root / ".arc/shared/state.json"
        self.model = os.environ.get("MODEL", "openai:gpt-5.4")

    async def _log(self, message: str, node_id: str) -> None:
        if self.log_cb:
            result = self.log_cb("SharedPreparation", message, "RUNNING", node_id)
            if inspect.isawaitable(result):
                await result

    def _bundle(self, feedback: str, required: list[str]):
        frontend = ["frontend"] if self.app_type == "web" else ["app/src/main"] if self.app_type == "android" else []
        for path in ("backend/package.json", "frontend/package.json", "app/__main__.py",
                     "app/main.py", "backend/src/database/db_runtime.js", "backend/src/database/index.js"):
            if (self.root / path).is_file():
                required.append(path)
        return source_bundle(self.root, list(dict.fromkeys(required)),
                             frontend_roots=frontend, design=True, feedback=feedback)

    def _validate(self, capability: Capability, state: dict[str, Any], runtime) -> None:
        if not capability.files and not capability.reuse_files:
            raise ValueError("Capability needs implementation files or existing reuse_files")
        existing = state.get("plan", {}).get("capabilities", [])
        if any(item["name"] == capability.name for item in existing):
            raise ValueError("Existing capability is immutable; reuse it or request a distinct adapter")
        occupied = {path for cap in existing for path in cap.get("files", []) + cap.get("reuse_files", [])}
        blocked = protected_paths(self.root, "GLOBAL:SHARED", runtime.traceability.list_interfaces())
        if len(set(capability.files)) != len(capability.files) or set(capability.files) & set(capability.reuse_files):
            raise ValueError("Duplicate or overlapping capability paths")
        for path in capability.files:
            safe_path(self.root, path)
            valid = (path.startswith(("backend/src/shared/", "frontend/src/shared/")) if self.app_type == "web"
                     else path.startswith("app/shared/") if self.app_type == "cli"
                     else path.startswith("app/src/main/java/") and "/shared/" in path)
            if not valid or path in occupied or path in blocked or is_test_asset(path):
                raise ValueError(f"Invalid new shared module: {path}")
        for path in capability.reuse_files:
            if not safe_path(self.root, path).is_file() or is_test_asset(path):
                raise ValueError(f"Missing/invalid read-only infrastructure: {path}")
            if any(record.get("file_path") == path and record.get("type") in {"API", "FUNC", "DB"}
                   and not str(record.get("interface_id", "")).startswith("GLOBAL:")
                   for record in runtime.traceability.list_interfaces()):
                raise ValueError(f"Feature-owned code cannot be claimed as shared: {path}")

    async def resolve(self, node_id: str, need: SharedNeed, runtime, app_handler) -> dict[str, Any]:
        state = read_json_file(self.state_path) or {"plan": {"capabilities": []}}
        catalog = state.get("plan", {}).get("capabilities", [])
        existing = next((cap for cap in catalog if cap["name"] == need.name), None)
        if existing:
            for path in existing.get("files", []) + existing.get("reuse_files", []):
                target = safe_path(self.root, path)
                expected = state.get("code_hashes", {}).get(path)
                if not target.is_file() or expected and hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                    raise ValueError(f"Registered shared code is missing/changed: {path}")
            existing["req_ids"] = sorted(set(existing.get("req_ids", [])) | {node_id})
            write_json_file(self.state_path, state)
            return state
        database = read_json_file(self.root / ".arc/database/state.json") or {}
        request_key = hashlib.sha256(json.dumps(need.model_dump(), sort_keys=True).encode()).hexdigest()
        pending = state.get("pending") or {}
        capability = None
        if pending.get("request_key") == request_key and pending.get("capability"):
            capability = Capability.model_validate(pending["capability"])
            self._validate(capability, state, runtime)
        feedback = str(pending.get("error") or "") if pending.get("request_key") == request_key else ""
        requested: list[str] = []
        discovery_reads: dict[str, Any] = {}
        implementation_reads: dict[str, Any] = {}
        if capability is None:
            for call in range(1, 4):
                await self._log(f"Discover {need.name}: call {call}/3.", node_id)
                try:
                    bundle = self._bundle(feedback + "\n" + str(requested), [])
                    response = await ask_with_reads(self.model, DISCOVERY_PROMPT, {
                        "model_stage": "SHARED_DISCOVERY", "node_id": node_id,
                        "shared_need": need.model_dump(), "requirement": runtime.traceability.get_requirement(node_id),
                        "shared_index": shared_index(self.root), "prepared_database": database.get("plan"),
                        "feedback": feedback, **bundle,
                    }, Discovery, root=self.root, budget=discovery_reads,
                        log=lambda message: self._log(message, node_id))
                    if response.database_gap:
                        if response.capability or response.read_files:
                            raise ValueError("database_gap must be returned alone")
                        raise DatabaseGap(response.database_gap)
                    if response.read_files:
                        if response.capability:
                            raise ValueError("read_files must not accompany a capability")
                        for path in response.read_files:
                            safe_path(self.root, path)
                        raise ValueError("Additional source requested: " + json.dumps(response.read_files))
                    if response.capability is None:
                        raise ValueError("Expected one shared capability")
                    if response.capability.name != need.name:
                        raise ValueError("Capability name must equal shared_need.name")
                    self._validate(response.capability, state, runtime)
                    capability = response.capability
                    break
                except DatabaseGap:
                    raise
                except Exception as exc:
                    feedback = str(exc)[:16000]
                    requested = list(dict.fromkeys([*requested, *feedback_source_paths(self.root, feedback)]))[:24]
            if capability is None:
                raise ValueError(f"Shared discovery exhausted 3 calls: {feedback}")
        state["pending"] = {"request_key": request_key, "node_id": node_id,
                            "need": need.model_dump(), "capability": capability.model_dump()}
        write_json_file(self.state_path, state)
        owned = set(capability.files)
        reused = set(capability.reuse_files)
        blocked = protected_paths(self.root, "GLOBAL:SHARED", runtime.traceability.list_interfaces()) | reused
        from core.sessions import load_node_session
        groups = load_node_session(node_id).get("file_groups") or {}
        blocked.update(path for layer in ("API", "FUNC", "DB") for path in groups.get(layer, []))
        for call in range(1, 4):
            await self._log(f"Implement {need.name}: call {call}/3.", node_id)
            try:
                bundle = self._bundle(feedback + "\n" + str(requested),
                                      sorted(path for path in owned | reused if (self.root / path).is_file()))
                edits = await ask_with_reads(self.model, IMPLEMENT_PROMPT, {
                    "model_stage": "SHARED_IMPLEMENT", "node_id": node_id,
                    "capability": capability.model_dump(), "shared_index": shared_index(self.root),
                    "requirement": runtime.traceability.get_requirement(node_id),
                    "prepared_database": database.get("plan"), "feedback": feedback,
                    "protected_files": sorted(blocked), **bundle,
                }, CodeEdits, root=self.root, budget=implementation_reads,
                    log=lambda message: self._log(message, node_id)) if owned else CodeEdits()
                if edits.shared_need:
                    raise ValueError("Nested shared requests are not supported; resolve this capability only")
                roots = ("backend/src/", "frontend/src/") if self.app_type == "web" else ("app/",)
                entry_names = {"app", "main", "index", "server", "router", "routes", "__main__"}
                def allowed(path: str) -> bool:
                    return path not in blocked and not is_test_asset(path) and (
                        path in owned or self.app_type == "web" and path in {"backend/package.json", "frontend/package.json"}
                        or path in bundle["sources"] and path.startswith(roots)
                        and Path(path).stem.lower() in entry_names)
                def validate() -> None:
                    for path in owned | reused:
                        if not safe_path(self.root, path).is_file() or not (self.root / path).read_text().strip():
                            raise ValueError(f"Shared implementation missing: {path}")
                apply_edits(self.root, edits, bundle["sources"], allowed, validate)
                state["pending"]["accepted_edits"] = edits.model_dump()
                write_json_file(self.state_path, state)
                output = await app_handler.run_build()
                codes = re.findall(r"^\s*Exit Code:\s*(-?\d+)\s*$", output, re.MULTILINE)
                if not codes or any(int(code) != 0 for code in codes):
                    raise ValueError("Shared build failed:\n" + output[-24000:]
                                     + "\nSource paths: " + json.dumps(feedback_source_paths(self.root, output)))
                exports: set[str] = set()
                for relative in owned | reused:
                    content = (self.root / relative).read_text(encoding="utf-8")
                    exports.update(re.findall(r"exports\.([A-Za-z_$][\w$]*)\s*=", content))
                    for body in re.findall(r"module\.exports\s*=\s*\{([^{}]*)\}", content):
                        exports.update(re.findall(r"(?:^|,)\s*([A-Za-z_$][\w$]*)\s*(?=[:,}]|$)", body))
                    exports.update(re.findall(r"export\s+(?:async\s+)?(?:function|class|const|let)\s+([A-Za-z_$][\w$]*)", content))
                    if relative.endswith(".py"):
                        exports.update(re.findall(r"^(?:async\s+)?def\s+([A-Za-z]\w*)\s*\(", content, re.MULTILINE))
                        exports.update(re.findall(r"^class\s+([A-Za-z]\w*)\s*[:(]", content, re.MULTILINE))
                    if relative.endswith(".java"):
                        exports.update(re.findall(r"public\s+(?:static\s+)?[\w<>?\[\]]+\s+(\w+)\s*\(", content))
                entry = {**capability.model_dump(), "req_ids": [node_id],
                         "purpose": need.reason[:160], "exports": sorted(exports)}
                state["plan"] = {"capabilities": [*catalog, entry]}
                state.setdefault("code_hashes", {}).update({
                    path: hashlib.sha256((self.root / path).read_bytes()).hexdigest() for path in owned | reused})
                state.update({"status": "COMPLETED", "error": ""})
                state.pop("pending", None)
                write_json_file(self.state_path, state)
                runtime.git.commit(f"ARC shared capability: {capability.name}")
                return state
            except Exception as exc:
                feedback = str(exc)[:32000]
                requested = list(dict.fromkeys([*requested, *feedback_source_paths(self.root, feedback)]))[:24]
                state["pending"].update({"call": call, "error": feedback})
                write_json_file(self.state_path, state)
        raise ValueError(f"Shared implementation exhausted 3 calls: {feedback}")


class DatabaseGap(ValueError):
    def __init__(self, detail: str):
        super().__init__("Shared capability needs a database plan correction; node schema writes are forbidden: " + detail)
