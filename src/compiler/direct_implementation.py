"""Direct source implementation used by the without-IR ablation.

The pass keeps only a small, compiler-owned file and unit association record.
It does not introduce a backend/frontend design IR: the model sees requirement
text, compact source/interface summaries, and returns files to reuse or create.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arc_agents.base import BaseStructuredAgent, JsonModel
from arcbench_agent_runtime.jsonio import write_json_atomic


DIRECT_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["files", "create_files", "units"],
    "properties": {
        "files": {
            "type": "array",
            "minItems": 0,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "reason"],
                "properties": {
                    "path": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "create_files": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "reason"],
                "properties": {
                    "path": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "units": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "name", "file", "export_name", "responsibility"],
                "properties": {
                    "kind": {"type": "string", "enum": ["API", "API_CLIENT", "SERVICE", "HANDLER", "STORE", "COMPONENT", "PAGE", "LAYOUT", "TYPE"]},
                    "name": {"type": "string"},
                    "file": {"type": "string"},
                    "export_name": {"type": "string"},
                    "responsibility": {"type": "string"},
                },
            },
        },
    },
}


DIRECT_PLAN_INSTRUCTIONS = """Plan source files for direct implementation of one requirement.
This is a without-IR ablation: do not invent modules, entities, IDs, routes, or
generated metadata. Prefer reusing files that appear in source_index. You may
request new source files in create_files when the requirement needs a new
independent module, API handler, service, React component, page, store, or shared
type. New files must be under backend/src, frontend/src, or shared/src and must
have a .ts, .tsx, .js, .jsx, .mjs, or .cjs extension.
Prefer an existing interface, handler, route, component, store, or service. Select the
smallest set of files that can implement the requirement, but preserve file
boundaries: one backend module/interface/handler per file, one frontend component
or page per .tsx/.jsx file, and one shared type module per file. Never put a new
module or component implementation into an index, app, router, barrel, or entry
file; those files may only receive imports, exports, and route composition.
Reuse an existing file only when its current symbols have the same responsibility.
Return both arrays even when one is empty, plus units describing every interface,
module, component, and page. Every unit must map to exactly one file from files
or create_files. Do not put a new implementation into
server.ts, App.tsx, an index/barrel file, or a router unless that file only needs
an import/export/route composition edit.
For every planned unit, `file` is the complete workspace-relative source path and
`export_name` is the exact symbol that the implementation model must export from
that file. Keep the mapping one-to-one: do not place two unrelated units in one
file, and do not describe a unit without a file. If a new unit must be connected
to the application, select the existing entry/router/barrel file in `files` only
for the import/export/route edit; put the unit body in its own `create_files`
entry. The implementation model will receive this mapping verbatim.
Do not select tests, node_modules, lockfiles, package metadata, or compiler
artifacts. Return JSON only.
"""


@dataclass(slots=True)
class DirectPlan:
    requirement_id: str
    files: list[str]
    reasons: dict[str, str] = field(default_factory=dict)
    units: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class DirectPlanningResult:
    plans: dict[str, DirectPlan]
    source_index: list[dict[str, Any]]
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.plans) and not self.errors


class DirectRequirementPlanner:
    """Associate requirements with source units without producing design IR."""

    def __init__(self, model: JsonModel, output_root: Path, *, trace=None, model_log=None) -> None:
        self.output_root = output_root.resolve()
        self._trace = trace
        self._external_model_log = model_log
        self._agent = BaseStructuredAgent(
            model,
            schema_name="arc_direct_requirement_files",
            instructions=DIRECT_PLAN_INSTRUCTIONS,
            output_schema=DIRECT_PLAN_SCHEMA,
            retries=2,
            trace=trace,
            model_log=model_log or self._write_model_log,
            agent_name="DirectRequirementPlanner",
        )

    def _write_model_log(self, payload: dict[str, Any]) -> None:
        if self._external_model_log is not None:
            self._external_model_log(payload)
            return
        root = self.output_root / ".arc" / "model_logs" / "direct_planning"
        root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"{time.time_ns() % 1_000_000_000:09d}Z"
        requirement_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(payload.get("requirement_id", "unknown")))
        attempt = int(payload.get("attempt", 0) or 0)
        path = root / f"{stamp}-{requirement_id}-attempt-{attempt}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def run(self, requirement_ir: dict[str, Any]) -> DirectPlanningResult:
        source_index = build_source_index(self.output_root)
        by_path = {row["path"]: row for row in source_index}
        plans: dict[str, DirectPlan] = {}
        errors: list[str] = []
        nodes = requirement_ir.get("nodes", {})
        order = requirement_ir.get("node_order", [])
        for requirement_id in order:
            requirement = nodes.get(requirement_id)
            if not isinstance(requirement, dict):
                continue
            payload = {
                "requirement_id": requirement_id,
                "requirement": requirement,
                # Keep the first pass compact. Previously associated files are
                # always visible for reuse; candidate summaries are limited to
                # the most relevant deterministic matches for this requirement.
                "source_index": _candidate_source_index(requirement, source_index, plans),
                "source_tree": sorted(row["path"] for row in source_index),
                "previous_associations": [
                    {"requirement_id": rid, "files": plan.files}
                    for rid, plan in plans.items()
                ],
                "iteration": 0,
            }

            def validate(output: dict[str, Any]) -> list[str]:
                rows = output.get("files") if isinstance(output, dict) else None
                if not isinstance(rows, list):
                    return ["files must be an array."]
                creates = output.get("create_files")
                if not isinstance(creates, list):
                    return ["create_files must be an array."]
                units = output.get("units")
                if not isinstance(units, list):
                    return ["units must be an array."]
                if not rows and not creates:
                    return ["files or create_files must contain at least one source file."]
                seen: set[str] = set()
                issues: list[str] = []
                for row in [*rows, *creates]:
                    if not isinstance(row, dict):
                        issues.append("Each files item must be an object.")
                        continue
                    path = str(row.get("path", "")).replace(chr(92), "/")
                    if path in seen:
                        issues.append(f"Duplicate file: {path}")
                    seen.add(path)
                    is_new = row in creates
                    if not is_new and path not in by_path:
                        issues.append(f"Unknown existing file {path!r}; choose only source_index paths.")
                    if is_new and path in by_path:
                        issues.append(f"create_files path already exists: {path!r}; move it to files.")
                    if path.startswith(("tests/", "node_modules/", ".arc/")) or not path.startswith(("backend/src/", "frontend/src/", "shared/src/")):
                        issues.append(f"Non-source file is not editable: {path}")
                allowed = seen
                for unit in units:
                    if not isinstance(unit, dict):
                        issues.append("Each units item must be an object.")
                        continue
                    unit_file = str(unit.get("file", "")).replace(chr(92), "/")
                    if unit_file not in allowed:
                        issues.append(f"Unit {unit.get('name', '')!r} must map to one planned file: {unit_file!r}.")
                    if unit_file.rsplit("/", 1)[-1].lower() in {"server.ts", "app.tsx", "app.jsx"} and unit.get("kind") not in {"TYPE", "LAYOUT"}:
                        issues.append(f"Unit {unit.get('name', '')!r} cannot implement business logic in entry file {unit_file!r}.")
                    export_name = str(unit.get("export_name", "")).strip()
                    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", export_name):
                        issues.append(f"Unit {unit.get('name', '')!r} has invalid export_name {export_name!r}.")
                if not units:
                    issues.append("units must describe at least one generated or reused unit.")
                return issues

            invocation = self._agent.invoke(payload, validate=validate)
            if invocation.ok and invocation.output:
                selected = [str(row["path"]).replace(chr(92), "/") for row in (
                    invocation.output["files"] + invocation.output["create_files"]
                )]
                plans[requirement_id] = DirectPlan(
                    requirement_id,
                    selected,
                    {str(row["path"]): str(row.get("reason", "")) for row in (
                        invocation.output["files"] + invocation.output["create_files"]
                    )},
                    list(invocation.output["units"]),
                )
                _ensure_source_files(self.output_root, invocation.output["create_files"])
                if invocation.output["create_files"]:
                    source_index = build_source_index(self.output_root)
                    by_path = {row["path"]: row for row in source_index}
                continue
            fallback = deterministic_files(requirement, source_index)
            if fallback:
                plans[requirement_id] = DirectPlan(requirement_id, fallback, {path: "deterministic fallback" for path in fallback})
            else:
                errors.append(f"{requirement_id}: direct file planning failed: {'; '.join(invocation.errors)}")
        return DirectPlanningResult(plans, build_source_index(self.output_root), errors)

    def write(self, result: DirectPlanningResult) -> Path:
        path = self.output_root / ".arc" / "direct" / "requirements.json"
        write_json_atomic(path, {
            "mode": "without_ir",
            "source_index": result.source_index,
            "requirements": [
                {"requirement_id": rid, "files": plan.files, "reasons": plan.reasons, "units": plan.units}
                for rid, plan in result.plans.items()
            ],
        })
        return path


def build_source_index(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for workspace in ("backend/src", "frontend/src", "shared/src"):
        base = root / workspace
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
                continue
            relative = path.relative_to(root).as_posix()
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            exports = sorted(set(re.findall(r"\b(?:export\s+(?:async\s+)?(?:function|class|const|type|interface)|export\s*\{)\s*([A-Za-z_$][\w$]*)", source)))[:40]
            symbols = sorted(set(re.findall(r"\b(?:function|class|interface|type|const)\s+([A-Za-z_$][\w$]*)", source)))[:60]
            rows.append({
                "path": relative,
                "workspace": relative.split("/", 1)[0],
                "role": _source_role(relative),
                "bytes": len(source.encode("utf-8")),
                "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "exports": exports,
                "symbols": symbols,
                "summary": " ".join(line.strip() for line in source.splitlines() if line.strip() and not line.lstrip().startswith(("//", "/*", "*")))[:700],
            })
    return rows


def _ensure_source_files(root: Path, rows: list[dict[str, Any]]) -> None:
    """Materialize planned source files before implementation context capture."""
    for row in rows:
        relative = str(row.get("path", "")).replace(chr(92), "/")
        target = (root / Path(*relative.split("/"))).resolve()
        if root not in target.parents or target.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_size == 0:
            target.write_text(f"// ARC_DIRECT_FILE: {relative}\n", encoding="utf-8")


def ensure_direct_test_runtime(root: Path) -> list[str]:
    """Provide test-support imports when database lowering is intentionally absent."""
    files = {
        "backend/src/db/client.js": (
            'import Database from "better-sqlite3";\n'
            'export const sqliteDatabase = new Database(":memory:");\n'
            'export const database = sqliteDatabase;\n'
            'export function resetDatabase() { sqliteDatabase.exec("PRAGMA foreign_keys = ON;"); }\n'
        ),
        "backend/src/db/client.d.ts": (
            'import type Database from "better-sqlite3";\n'
            'export const sqliteDatabase: Database.Database;\n'
            'export const database: Database.Database;\n'
            'export function resetDatabase(): void;\n'
        ),
        "backend/database-baseline.mjs": (
            'export function resetToBaseline(database) {\n'
            '  try { database.exec("PRAGMA foreign_keys = ON;"); } catch {}\n'
            '}\n'
        ),
        "backend/database-baseline.d.mts": (
            'import type Database from "better-sqlite3";\n'
            'export function resetToBaseline(database: Database.Database): void;\n'
        ),
    }
    created: list[str] = []
    for relative, content in files.items():
        target = root / Path(*relative.split("/"))
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append(relative)
    return created


def _source_role(relative: str) -> str:
    """Classify a file for planning without introducing a design IR."""
    path = relative.lower()
    name = path.rsplit("/", 1)[-1]
    if name in {"index.ts", "index.tsx", "main.ts", "main.tsx", "app.ts", "app.tsx"}:
        return "entrypoint"
    if "router" in name or "/routes/" in path:
        return "route_composition"
    if path.startswith("frontend/"):
        return "component_or_page"
    if path.startswith("backend/"):
        return "backend_module"
    if path.startswith("shared/"):
        return "shared_module"
    return "source_module"


def deterministic_files(requirement: dict[str, Any], source_index: list[dict[str, Any]]) -> list[str]:
    text = str(requirement).lower()
    scored: list[tuple[int, str]] = []
    for row in source_index:
        path = row["path"]
        score = sum(2 for token in re.findall(r"[a-z0-9]+", text) if token in path.lower())
        if any(token in text for token in ("page", "screen", "ui", "frontend", "route", "component")) and row["workspace"] == "frontend":
            score += 3
        if any(token in text for token in ("api", "service", "backend", "data", "persist", "database")) and row["workspace"] == "backend":
            score += 3
        scored.append((score, path))
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = [path for score, path in scored if score > 0][:3]
    if selected:
        return selected
    preferred = ["backend/src/app.ts", "frontend/src/app/router.tsx", "frontend/src/App.tsx"]
    return [path for path in preferred if any(row["path"] == path for row in source_index)][:1]


def _candidate_source_index(
    requirement: dict[str, Any],
    source_index: list[dict[str, Any]],
    plans: dict[str, DirectPlan],
) -> list[dict[str, Any]]:
    text = str(requirement).lower()
    prior = {path for plan in plans.values() for path in plan.files}
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in source_index:
        path = str(row["path"])
        score = 10 if path in prior else 0
        score += sum(2 for token in re.findall(r"[a-z0-9]+", text) if token in path.lower())
        if row["workspace"] == "frontend" and any(token in text for token in ("page", "screen", "ui", "frontend", "route", "component")):
            score += 4
        if row["workspace"] == "backend" and any(token in text for token in ("api", "service", "backend", "data", "persist", "database")):
            score += 4
        scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], str(item[1]["path"])))
    selected = [row for score, row in scored if score > 0][:40]
    if not selected:
        selected = [row for _, row in scored[:40]]
    return selected


def direct_binding_registry(result: DirectPlanningResult) -> dict[str, Any]:
    """Create a transient resolver registry from direct file associations."""
    rows_by_path = {row["path"]: row for row in result.source_index}
    ids = {path: "DIRECT.FILE." + hashlib.sha1(path.encode()).hexdigest()[:12] for path in rows_by_path}
    bindings: list[dict[str, Any]] = []
    for path, row in rows_by_path.items():
        kind = "COMPONENT" if row["workspace"] == "frontend" else "FUNC"
        for plan in result.plans.values():
            for unit in plan.units:
                if str(unit.get("file", "")).replace(chr(92), "/") == path:
                    kind = str(unit.get("kind", kind))
                    break
        bindings.append({"module_id": ids[path], "kind": kind, "file": path, "symbol": None,
                         "input_type": None, "output_type": None, "props_type": None,
                         "route": None, "callees": []})
    requirement_targets = []
    direct_units: dict[str, list[dict[str, Any]]] = {}
    for rid, plan in result.plans.items():
        writable = [ids[path] for path in plan.files if path in ids]
        requirement_targets.append({"requirement_id": rid, "writable": writable, "read_only": []})
        direct_units[rid] = plan.units
    return {"status": "CODE_BINDING_READY", "code_bindings": bindings,
            "type_bindings": [], "requirement_targets": requirement_targets,
            "direct_units": direct_units}


def direct_source_registry(root: Path, requirement_ids: list[str]) -> dict[str, Any]:
    """Build a lightweight source-to-requirement registry without a planner call."""
    rows = build_source_index(root)
    bindings: list[dict[str, Any]] = []
    ids: list[str] = []
    for row in rows:
        path = str(row["path"])
        module_id = "DIRECT.FILE." + hashlib.sha1(path.encode()).hexdigest()[:12]
        ids.append(module_id)
        bindings.append({
            "module_id": module_id,
            "owner_requirements": [],
            "kind": "COMPONENT" if row["workspace"] == "frontend" else "FUNC",
            "file": path, "symbol": None, "input_type": None,
            "output_type": None, "props_type": None, "route": None, "callees": [],
        })
    return {
        "status": "CODE_BINDING_READY",
        "code_bindings": bindings,
        "type_bindings": [],
        "requirement_targets": [
            {"requirement_id": str(rid), "writable": ids, "read_only": []}
            for rid in requirement_ids
        ],
        "direct_units": {},
    }
