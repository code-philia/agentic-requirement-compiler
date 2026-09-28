"""Requirement-scheduled UI, assembly and behavior passes for the seven-entity IR."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.logging import SynchronousLog
from arcbench_agent_runtime.jsonio import write_json_atomic

from .design_projection import project_api_contracts
from .frontend_generation_contracts import (
    BEHAVIOR_INSTRUCTIONS, REQUIREMENT_ASSEMBLY_INSTRUCTIONS,
    UI_INSTRUCTIONS, ShapeError, BatchValidationError, collect_shape_errors, check_shape,
    normalize_patch, patch_schema, protocol_examples,
)
from .frontend_workspace import FrontendWorkspace, references
from .frontend_protocol import (
    project_context, check_references, repair_context, apply_repairs,
    REPAIR_SCHEMA, REPAIR_INSTRUCTIONS,
)
from .model_client import StructuredModel, describe_model_error
from .visual_reference import ResolvedVisualReference, VisualModel, VisualStructuredModel


MAX_INPUT_CHARS = 24000
RETRY_RESERVE = 1800
# Text envelope including instructions and schema, independent of image bytes/provider tokens.
MAX_REQUEST_CHARS = 100000
MAX_OUTPUT_CHARS = 20000
MAX_EDITS = 32
TEXT_CHUNK = 3500
CATALOG_SIZE = 6


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def brief(row: dict[str, Any]) -> dict[str, Any]:
    return {key: (value[:220] if isinstance(value, str) else value)
            for key, value in row.items() if key in {
                "id", "name", "kind", "spec", "owner_id", "component_id", "component_ref", "ui_root_id",
            }}


def ranked(rows: list[dict[str, Any]], query: Any, count: int) -> list[dict[str, Any]]:
    """Small deterministic retrieval index; selection never removes scheduled source tasks."""
    text = encoded(query).lower()
    terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text))
    def score(row: dict[str, Any]) -> int:
        other = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", encoded(row).lower()))
        return len(terms & other)
    return sorted(rows, key=lambda row: (-score(row), str(row.get("id", ""))))[:count]


def ui_data_associations(frontend_ir: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """Derived lookup, never a second model-authored source of UI binding truth."""
    data = {row["id"]: row for row in frontend_ir.get("data", [])}
    properties = {row["id"]: row for row in frontend_ir.get("properties", [])}
    values = {**data, **properties}
    ui_rows = {row["id"]: row for row in frontend_ir.get("ui", [])}
    def item_sources(value: Any) -> set[str]:
        if isinstance(value, list):
            return {uid for child in value for uid in item_sources(child)}
        if not isinstance(value, dict) or value.get("kind") == "LITERAL":
            return set()
        found = {value["ui_id"]} if value.get("kind") == "ITEM" else set()
        return found | {uid for child in value.values() for uid in item_sources(child)}
    result = {}
    for ui in frontend_ir.get("ui", []):
        direct = {target for _, target in references(ui) if target in values}
        sources, source_pending = set(), list(item_sources(ui))
        seen = set(direct)
        while source_pending:
            uid = source_pending.pop()
            if uid in sources:
                continue
            sources.add(uid)
            source = (ui_rows.get(uid, {}).get("repeat") or {}).get("source")
            seen.update(target for _, target in references(source) if target in values)
            source_pending.extend(item_sources(source) - sources)
        pending = list(seen)
        while pending:
            for _, target in references(values[pending.pop()]):
                if target in values and target not in seen:
                    seen.add(target)
                    pending.append(target)
        result[ui["id"]] = {"direct_ids": sorted(direct), "repeat_ui_ids": sorted(sources),
                            "data_ids": sorted(seen & data.keys()),
                            "property_ids": sorted(seen & properties.keys())}
    return result


@dataclass
class FrontendIRGenerationResult:
    frontend_ir: dict[str, Any]
    report: dict[str, Any]
    batches: list[dict[str, Any]] = field(default_factory=list)
    traceability: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.report["status"] in {"GENERATED", "GENERATED_WITH_WARNINGS"}


class FrontendIRGenerationPass:
    def __init__(self, model: StructuredModel, artifact_root: Path,
                 visual_model: VisualStructuredModel | None = None) -> None:
        self.model = model
        self.traceability_path = artifact_root / "design" / "frontend" / "traceability.json"
        self.visual_model = visual_model
        self.log = SynchronousLog("FrontendIRGenerationPass", workspace_root=artifact_root.resolve().parent)

    def compile(self, requirement_ir: dict[str, Any], dependency_graph: dict[str, Any],
                backend_design_ir: dict[str, Any],
                visual_references: list[ResolvedVisualReference | dict[str, Any]],
                visual_analyses: list[dict[str, Any]] | None = None) -> FrontendIRGenerationResult:
        self.workspace = FrontendWorkspace()
        self.batches: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.nodes = requirement_ir.get("nodes", {})
        self.visual_analyses = {row["id"]: row for row in visual_analyses or []}
        self.traceability = {rid: {"mode": "DESIGN"} for rid in self.nodes}
        self.dependencies = dependency_graph.get("requirement_dependencies", {})
        self.apis = {row["id"]: row for row in project_api_contracts(backend_design_ir)}
        self.api_owners: dict[str, set[str]] = {}
        for row in backend_design_ir.get("requirements", []):
            self.api_owners[str(row.get("id", ""))] = set(row.get("api_ids", []))
        for row in backend_design_ir.get("modules", []):
            if row.get("kind") == "API":
                self.api_owners.setdefault(str(row.get("owner_requirement", "")), set()).add(row["id"])
        ordered, visiting, visited = [], set(), set()
        def visit(rid: str) -> None:
            if rid in visited or rid not in self.nodes:
                return
            if rid in visiting:
                raise ValueError(f"Requirement dependency/parent cycle at {rid}")
            visiting.add(rid)
            children = [key for key, node in self.nodes.items() if node.get("parent_id") == rid]
            for dependency in dict.fromkeys([*self.dependencies.get(rid, []), *children]):
                visit(dependency)
            visiting.remove(rid)
            visited.add(rid)
            ordered.append(rid)
        for rid in [*requirement_ir.get("node_order", []), *self.nodes]:
            visit(rid)
        root_id = requirement_ir.get("root_id")
        if ordered:
            self.workspace.ensure_root([root_id] if root_id in self.nodes else [ordered[0]])

        # Three requirement traversals. No per-observation, per-UI or per-component model loop.
        tasks = []
        for rid in ordered:
            node = self.nodes[rid]
            source = str(node.get("description", "")) + "\n" + encoded(node.get("scenarios", []))
            fragments = [source[i:i + TEXT_CHUNK] for i in range(0, len(source), TEXT_CHUNK)] or [""]
            visuals = [v for v in visual_references
                       if rid in (v.get("requirement_ids", []) if isinstance(v, dict) else v.requirement_ids)]
            for index, fragment in enumerate(fragments):
                tasks.append({"id": rid, "name": node.get("name", ""), "fragment": fragment,
                              "part": index + 1, "parts": len(fragments), "has_visual": bool(visuals),
                              "visual": visuals[0] if visuals else None})
            # The visual interface accepts one image. Extra images remain supplements of this requirement.
            for visual in visuals[1:]:
                tasks.append({"id": rid, "name": node.get("name", ""), "fragment": source[:TEXT_CHUNK],
                              "visual": visual, "has_visual": True, "visual_supplement": True})
        failed_ui: set[str] = set()
        for task in tasks:
            if not self._requirement_task("ui", task):
                failed_ui.add(task["id"])
        failed_assemble: set[str] = set()
        for stage in ("assemble", "behavior"):
            for task in tasks:
                rid = task["id"]
                if stage == "behavior" and task.get("visual_supplement"):
                    continue
                if rid in failed_ui:
                    self._fail(stage, [rid], "Requirement UI generation failed; dependent task skipped.")
                    continue
                if stage == "behavior" and rid in failed_assemble:
                    self._fail(stage, [rid], "Requirement assembly failed; behavior task skipped.")
                    continue
                if self.traceability[rid]["mode"] == "SUMMARY":
                    if stage == "assemble":
                        linked = self.traceability[rid].get("ui_ids", [])
                        components = {self.workspace.owner(eid) for eid in linked}
                        for child in self.nodes:
                            if self.nodes[child].get("parent_id") == rid:
                                components.update(self.traceability[child].get("component_ids", []))
                        self.workspace.apply_requirement_batch(
                            {"creates": [], "updates": [], "associations": [{"id": cid} for cid in sorted(components)[:8]]},
                            [rid], set(), stage)
                        self._refresh_traceability()
                    continue
                component_roots = {c["ui_root_id"] for c in self.workspace.tables["components"].values()}
                if not any(rid in row["requirement_ids"] for table, rows in self.workspace.tables.items()
                           for row in rows.values() if table != "components" and row["id"] not in component_roots):
                    continue
                if not self._requirement_task(stage, task) and stage == "assemble":
                    failed_assemble.add(rid)

        self.warnings.extend(self.workspace.inspect(set(self.apis)))
        status = "PARTIAL" if self.failures else ("GENERATED_WITH_WARNINGS" if self.warnings else "GENERATED")
        if not self.nodes:
            status = "FAILED"
            self.warnings.append("No requirements were supplied.")
        report = {"status": status, "warnings": list(dict.fromkeys(self.warnings)),
                  "failed_tasks": self.failures,
                  "task_counts": {phase: sum(b["phase"] == phase for b in self.batches)
                                  for phase in ("ui", "assemble", "behavior")},
                  "requirement_entities": {rid: [
                      row["id"] for rows in self.workspace.tables.values() for row in rows.values()
                      if rid in row["requirement_ids"]
                  ] for rid in ordered}}
        self._refresh_traceability(status)
        return FrontendIRGenerationResult(self.workspace.export(), report, self.batches, self.traceability)

    def _refresh_traceability(self, status: str = "IN_PROGRESS") -> None:
        links = frontend_ir_traceability(self.workspace.export())
        for rid in self.nodes:
            mode = self.traceability[rid]["mode"]
            self.traceability[rid] = {"mode": mode,
                                      **{key: [] for key in ("component_ids", "ui_ids", "data_ids", "property_ids",
                                                            "event_ids", "handler_ids", "effect_ids")},
                                      **links.get(rid, {})}
        write_json_atomic(self.traceability_path, {"status": status, "requirements": self.traceability,
                                                   "ui_data": ui_data_associations(self.workspace.export())})

    def _requirement_task(self, stage: str, task: dict[str, Any],
                          focus_ids: list[str] | None = None, depth: int = 0) -> bool:
        rid = task["id"]
        requirement = {k: v for k, v in task.items() if k != "visual"}
        local = self._requirement_context(rid, requirement, focus_ids)
        context = {**local, "requirement": requirement,
                   "child_requirements": [{"id": child, "name": node.get("name", ""),
                                            "description": str(node.get("description", ""))[:500],
                                            "links": self.traceability[child]}
                                           for child, node in self.nodes.items() if node.get("parent_id") == rid],
                   "related_requirements": self._requirement_summaries([rid]),
                   "api_contracts": self._api_context([rid], requirement) if stage != "assemble" else []}
        if stage in {"assemble", "behavior"}:
            ui_data = ui_data_associations(self.workspace.export())
            context["ui_data"] = {row["id"]: ui_data[row["id"]] for row in local["entities"] if row["id"] in ui_data}
        example = protocol_examples("C.EXAMPLE", "U.EXAMPLE")
        example["associations"] = []
        if stage == "ui":
            example["requirement_mode"] = "DESIGN"
        if stage == "assemble":
            example["components"] = []
        if stage == "behavior":
            example["creates"].update(events=[], handlers=[], effects=[])
        context["protocol_example"] = example
        image = None
        visual = task.get("visual") if stage in {"ui", "assemble"} else None
        if isinstance(visual, dict):
            context["visual_analysis"] = visual.get("analysis", {})
            context["visual_reference_id"] = visual.get("id")
            context["visual_reference_path"] = visual.get("source_path")
        elif visual is not None:
            try:
                content = visual.absolute_path.read_bytes()
                if hashlib.sha256(content).hexdigest() != visual.sha256:
                    raise ValueError("Visual reference changed after resolution")
                image = f"data:{visual.media_type};base64,{base64.b64encode(content).decode('ascii')}"
                context["visual_reference_id"] = visual.id
                context["visual_reference_path"] = visual.source_path
                context["visual_analysis"] = self.visual_analyses.get(visual.id, {}).get("analysis", {})
            except (OSError, ValueError) as exc:
                self.warnings.append(f"Visual {visual.id} unavailable: {exc}")
        # Split only an oversized requirement context, never one request per ordinary UI node.
        focus = local["focus_ids"]
        projected_size = len(encoded(project_context({k: v for k, v in context.items() if k != "protocol_example"}))) + len(encoded(context.get("protocol_example", {})))
        if projected_size > MAX_INPUT_CHARS - RETRY_RESERVE and len(focus) > 1 and depth < 5:
            midpoint = len(focus) // 2
            left = self._requirement_task(stage, task, focus[:midpoint], depth + 1)
            right = self._requirement_task(stage, task, focus[midpoint:], depth + 1)
            return left and right
        instructions = {"ui": UI_INSTRUCTIONS, "assemble": REQUIREMENT_ASSEMBLY_INSTRUCTIONS,
                        "behavior": BEHAVIOR_INSTRUCTIONS}[stage]
        def apply(batch: dict[str, Any]) -> dict[str, Any]:
            mode = batch.get("requirement_mode", "DESIGN")
            if stage == "ui" and mode == "SUMMARY" and (
                task.get("has_visual") or batch["creates"] or batch["updates"] or not context["child_requirements"]
            ):
                raise ValueError("SUMMARY requires a parent without images and empty creates/updates")
            result = self.workspace.apply_requirement_batch(batch, [rid], set(local["editable_ids"]), stage)
            if stage == "ui":
                previous = self.traceability[rid].get("mode")
                self.traceability[rid]["mode"] = "DESIGN" if task.get("part", 1) > 1 and previous == "DESIGN" else mode
            self._refresh_traceability()
            return result
        return self._call(stage, patch_schema(stage), instructions, context, [rid], apply, image=image)

    def _requirement_context(self, rid: str, query: Any,
                             focus_ids: list[str] | None) -> dict[str, Any]:
        ws = self.workspace
        all_rows = {eid: row for rows in ws.tables.values() for eid, row in rows.items()}
        roots = {c["ui_root_id"] for c in ws.tables["components"].values()}
        linked_ids = {eid for key, values in self.traceability[rid].items() if key.endswith("_ids") for eid in values}
        owned = [row for row in all_rows.values() if row["id"] in linked_ids
                 and row["id"] not in roots and row["id"] not in ws.tables["components"]]
        focus = focus_ids if focus_ids is not None else [row["id"] for row in owned]
        selected = set(focus)
        # Existing matching UI/state is reusable. Retrieval does not schedule extra model calls.
        if focus_ids is None:
            related = [row for row in all_rows.values()
                       if row["id"] not in selected and row["id"] not in roots]
            selected.update(row["id"] for row in ranked(related, query, 8))
        # Extracted subtrees keep their identity; include descendants and data/behavior contracts.
        pending = list(selected)
        while pending:
            eid = pending.pop()
            row = all_rows.get(eid)
            if row is None:
                continue
            for field, target in references(row):
                if field in {"component_id", "owner_id", "component_ref", "ui_root_id"}:
                    continue
                if focus_ids is not None and field == "children":
                    continue
                if target in all_rows and target not in selected:
                    selected.add(target)
                    pending.append(target)
        owners = {ws.owner(eid) for eid in selected} | {ws.root_component_id}
        for cid in owners:
            if cid in ws.tables["components"]:
                selected.add(cid)
                selected.add(ws.tables["components"][cid]["ui_root_id"])
        # Parent use-sites and child inputs/events allow same-response cross-component wiring.
        for row in ws.tables["ui"].values():
            if row["kind"] == "COMPONENT" and (row["id"] in selected or row["component_ref"] in owners):
                selected.add(row["id"])
                selected.add(row["component_id"])
                selected.add(row["component_ref"])
                selected.add(ws.tables["components"][row["component_id"]]["ui_root_id"])
        for row in ws.tables["events"].values():
            if row["component_id"] in selected and row["kind"] == "CUSTOM":
                selected.add(row["id"])
        for row in ws.tables["data"].values():
            if row["owner_id"] in selected:
                selected.add(row["id"])
        return {"default_component_id": ws.root_component_id, "focus_ids": focus,
                "split_requirement": focus_ids is not None,
                "entities": [copy.deepcopy(all_rows[eid]) for eid in sorted(selected) if eid in all_rows],
                "editable_ids": sorted(eid for eid in selected if eid in all_rows),
                "component_catalog": [self._component_summary(row) for row in ranked(
                    list(ws.tables["components"].values()), query, CATALOG_SIZE)]}

    def _component_summary(self, component: dict[str, Any]) -> dict[str, Any]:
        """Bounded reuse index; full records and mutation rights stay in the local context."""
        cid = component["id"]
        requirements = [rid for rid, links in self.traceability.items()
                        if cid in links.get("component_ids", [])]
        inputs = [row for row in self.workspace.tables["data"].values() if row["owner_id"] == cid]
        events = [row for row in self.workspace.tables["events"].values()
                  if row["component_id"] == cid and row["kind"] == "CUSTOM"]
        use_sites = [row for row in self.workspace.tables["ui"].values()
                     if row["kind"] == "COMPONENT" and row["component_ref"] == cid]
        root = self.workspace.tables["ui"][component["ui_root_id"]]
        return {**brief(component),
                "requirements": [{"id": rid, "name": self.nodes[rid].get("name", ""),
                                  "summary": str(self.nodes[rid].get("description", ""))[:160]}
                                 for rid in requirements[:3]],
                "render_roots": [brief(self.workspace.tables["ui"][uid]) for uid in root["children"][:4]],
                "inputs": [{"id": row["id"], "name": row["name"], "required": row["required"]} for row in inputs[:6]],
                "custom_events": [{"id": row["id"], "name": row["event_name"]} for row in events[:4]],
                "use_sites": [{"id": row["id"], "component_id": row["component_id"]} for row in use_sites[:4]],
                "counts": {"requirements": len(requirements), "render_roots": len(root["children"]),
                           "inputs": len(inputs), "custom_events": len(events), "use_sites": len(use_sites)}}

    def _call(self, phase: str, schema: dict[str, Any], instructions: str, context: dict[str, Any],
              requirements: list[str], apply: Callable[[dict[str, Any]], Any],
              image: str | None = None) -> bool:
        # Examples already use the model protocol; project only canonical context entities.
        context = {**project_context({k: v for k, v in context.items() if k != "protocol_example"}),
                   "protocol_example": context["protocol_example"]}
        context["compiler_fields"] = {
            "creates": {"id": "Omit or null; compiler always allocates the ID.",
                        "requirement_ids": "Omit or echo; compiler uses current requirement attribution."},
            "updates": {"id": "Required existing identity; not editable.",
                        "requirement_ids": "Omit or echo; compiler preserves and merges attribution.",
                        "component_id": "Copy existing ownership; moves require assembly actions.",
                        "ui_root_id": "Component root is compiler-owned; omit or echo."},
            "records": "Include the full entity field set for every kind; inactive fields are null or [].",
        }
        task_id = f"{phase}-{len(self.batches) + 1:05d}"
        record: dict[str, Any] = {"task_id": task_id, "phase": phase,
                                  "requirement_ids": requirements, "input": copy.deepcopy(context), "attempts": []}
        self.batches.append(record)
        if len(encoded(context)) > MAX_INPUT_CHARS - RETRY_RESERVE:
            self._fail(phase, requirements, "Local context exceeds the input budget; task retained as incomplete.", task_id)
            record["status"] = "FAILED"
            return False
        feedback: dict[str, Any] | None = None
        candidate: dict[str, Any] | None = None
        last_error = ""
        for attempt in range(2):
            repairing = attempt > 0 and candidate is not None
            payload = repair_context(candidate, feedback or {}, context) if repairing else copy.deepcopy(context)
            if feedback and not repairing:
                payload["repair_feedback"] = feedback
            call_schema = REPAIR_SCHEMA if repairing else schema
            call_instructions = REPAIR_INSTRUCTIONS if repairing else instructions
            if repairing:
                call_instructions += "\nThe frozen candidate uses this schema; replacement records must conform to their corresponding branch. This is NOT your response schema:\n" + encoded(schema)
            request_chars = len(encoded(payload)) + len(call_instructions) + len(encoded(call_schema))
            if len(encoded(payload)) > MAX_INPUT_CHARS or request_chars > MAX_REQUEST_CHARS:
                last_error = "Local request exceeds payload/text-envelope budget; no contract was truncated."
                record["attempts"].append({"error": last_error, "input_chars": len(encoded(payload)), "request_chars": request_chars})
                break
            self.log.info(f"MODEL_REQUEST phase=frontend_{phase} task={task_id} requirements={requirements} attempt={attempt + 1}/2 repair={repairing} input_chars={len(encoded(payload))} request_chars={request_chars}")
            raw = None
            attempt_record: dict[str, Any] = {"input": payload, "repair": repairing, "request_chars": request_chars}
            try:
                kwargs = dict(schema_name=f"frontend_{phase}" + ("_repair" if repairing else ""), instructions=call_instructions,
                              input_payload=payload, output_schema=call_schema)
                if image is not None and not repairing:
                    if self.visual_model is None:
                        self.visual_model = VisualModel.from_env()
                    raw = self.visual_model.generate_visual_json(**kwargs, image_data_url=image)
                else:
                    raw = self.model.generate_json(**kwargs)
                client = self.visual_model if image is not None and not repairing else self.model
                attempt_record["structured_output_mode"] = getattr(client, "structured_output_mode", "unknown")
                if len(encoded(raw)) > MAX_OUTPUT_CHARS:
                    raise ValueError("Output too large; return concise edits for this requirement, not the whole application.")
                if repairing:
                    check_shape(raw, REPAIR_SCHEMA)
                    candidate = apply_repairs(candidate, raw, payload)
                else:
                    candidate = copy.deepcopy(raw)
                # One protocol only: no aliases, field renaming, or literal-format coercion.
                if len(encoded(candidate)) > MAX_OUTPUT_CHARS:
                    raise ValueError("Repaired candidate exceeds the local output budget")
                errors = collect_shape_errors(candidate, schema)
                errors.extend(check_references(
                    candidate, {eid for rows in self.workspace.tables.values() for eid in rows},
                    set(context.get("editable_ids", []))))
                if errors:
                    raise BatchValidationError(errors)
                batch = normalize_patch(candidate)
                if len(batch["creates"]) + len(batch["updates"]) + len(batch.get("components", [])) > MAX_EDITS:
                    raise ValueError(f"Return at most {MAX_EDITS} local edits for this task.")
                result = apply(batch)
                record["attempts"].append({**attempt_record, "output": raw, "candidate": candidate, "status": "APPLIED", "merge": result})
                record["status"] = "APPLIED"
                self.log.info(f"MODEL_APPLIED phase=frontend_{phase} task={task_id} requirements={requirements} mode={attempt_record['structured_output_mode']} output_chars={len(encoded(raw))}")
                return True
            except Exception as exc:
                last_error = describe_model_error(exc)
                feedback = exc.feedback() if isinstance(exc, ShapeError) else {
                    "error": last_error[:900],
                    "hint": "Use {local: key} for new references and {id: supplied_id} for existing references. "
                            "Preserve valid unrelated records.",
                }
                # Malformed top-level structures have no safe record-level repair target.
                if candidate is not None and not (
                    isinstance(candidate, dict) and isinstance(candidate.get("creates"), dict)
                    and all(isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
                            for rows in candidate["creates"].values())
                    and all(isinstance(candidate.get(k, []), list) and all(isinstance(row, dict) for row in candidate.get(k, []))
                            for k in ("updates", "components"))
                ):
                    candidate = None
                shape_errors = exc.errors if isinstance(exc, BatchValidationError) else [exc]
                if any(isinstance(error, ShapeError) and error.path in {
                    "$", "$.creates", "$.updates", "$.components",
                    *{f"$.creates.{table}" for table in self.workspace.tables},
                } for error in shape_errors):
                    candidate = None
                client = self.visual_model if image is not None and not repairing else self.model
                attempt_record["structured_output_mode"] = getattr(client, "structured_output_mode", "unknown")
                record["attempts"].append({**attempt_record, "output": raw, "error": last_error, "repair_feedback": feedback})
                label = "MODEL_RETRY" if attempt == 0 else "MODEL_FAILED"
                self.log.info(f"{label} phase=frontend_{phase} task={task_id} requirements={requirements} error={last_error}")
        record["status"] = "FAILED"
        self._fail(phase, requirements, last_error, task_id)
        return False

    def _fail(self, phase: str, requirements: list[str], message: str, task_id: str = "") -> None:
        self.failures.append({"task_id": task_id, "phase": phase,
                              "requirement_ids": requirements, "message": message})

    def _requirement_summaries(self, requirements: list[str]) -> list[dict[str, Any]]:
        ids = list(requirements)
        for rid in requirements:
            parent = self.nodes.get(rid, {}).get("parent_id")
            if parent:
                ids.append(parent)
            ids.extend(self.dependencies.get(rid, []))
        return [{"id": rid, "name": self.nodes[rid].get("name", ""),
                 "description": self.nodes[rid].get("description", "")[:700]}
                for rid in list(dict.fromkeys(ids))[:8] if rid in self.nodes]

    def _api_context(self, requirements: list[str], query: Any, *, full: bool = True) -> list[dict[str, Any]]:
        ids: set[str] = set()
        for rid in requirements:
            for related in [rid, *self.dependencies.get(rid, [])]:
                ids.update(self.api_owners.get(related, set()))
        candidates = [self.apis[key] for key in sorted(ids) if key in self.apis]
        if not candidates:
            candidates = ranked(list(self.apis.values()), query, 4)
        selected = ranked(candidates, query, 4)
        return [copy.deepcopy(row) if full else {"id": row["id"], "spec": row["spec"][:250]} for row in selected]


def frontend_ir_traceability(frontend_ir: dict[str, Any]) -> dict[str, dict[str, Any]]:
    links: dict[str, dict[str, Any]] = {}
    for table, field_name in (("components", "component_ids"), ("ui", "ui_ids"),
                              ("data", "data_ids"), ("properties", "property_ids"),
                              ("events", "event_ids"), ("handlers", "handler_ids"), ("effects", "effect_ids")):
        for row in frontend_ir.get(table, []):
            for rid in row["requirement_ids"]:
                link = links.setdefault(rid, {"ui_scope": "UI_REQUIRED"})
                link.setdefault(field_name, []).append(row["id"])
    # Reusing an existing UI also associates its current owning component.
    for row in frontend_ir.get("ui", []):
        for rid in row["requirement_ids"]:
            components = links[rid].setdefault("component_ids", [])
            if row["component_id"] not in components:
                components.append(row["component_id"])
    return links
