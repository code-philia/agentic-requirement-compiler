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
    UI_INSTRUCTIONS, ShapeError, BatchValidationError, collect_shape_errors,
    normalize_patch, patch_schema, protocol_examples,
)
from .frontend_workspace import FrontendWorkspace, references
from .frontend_protocol import project_context, check_references, check_request_targets
from .model_client import StructuredModel, describe_model_error
from .visual_reference import ResolvedVisualReference, VisualModel, VisualStructuredModel


MODEL_RETRIES = 2
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
        self.artifact_root = artifact_root
        if hasattr(model, "set_usage_path"):
            model.set_usage_path(artifact_root / "model_usage.jsonl")
        self.traceability_path = artifact_root / "design" / "frontend" / "requirements.json"
        self.visual_model = visual_model
        self.log = SynchronousLog("FrontendIRGenerationPass", workspace_root=artifact_root.resolve().parent)

    def compile(self, requirement_ir: dict[str, Any], dependency_graph: dict[str, Any],
                backend_design_ir: dict[str, Any],
                visual_references: list[ResolvedVisualReference | dict[str, Any]],
                visual_analyses: list[dict[str, Any]] | None = None) -> FrontendIRGenerationResult:
        self.workspace = FrontendWorkspace()
        self.batches: list[dict[str, Any]] = []
        self.task_count = 0
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
            fragments = [source]
            visuals = [v for v in visual_references
                       if rid in (v.get("requirement_ids", []) if isinstance(v, dict) else v.requirement_ids)]
            for index, fragment in enumerate(fragments):
                tasks.append({"id": rid, "name": node.get("name", ""), "fragment": fragment,
                              "part": index + 1, "parts": len(fragments), "has_visual": bool(visuals),
                              "visual": visuals[0] if visuals else None})
            # The visual interface accepts one image. Extra images remain supplements of this requirement.
            for visual in visuals[1:]:
                tasks.append({"id": rid, "name": node.get("name", ""), "fragment": source,
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
                if self.traceability[rid]["mode"] == "NONE":
                    continue
                if rid in failed_ui:
                    self.warnings.append(f"{rid}: UI task failed; {stage} uses the committed IR where available.")
                if stage == "behavior" and rid in failed_assemble:
                    self.warnings.append(f"{rid}: assembly task failed; behavior uses the committed component structure.")
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
                linked_ui = set(self.traceability[rid].get("ui_ids", [])) - component_roots
                if stage == "assemble" and not linked_ui:
                    continue
                if not self._requirement_task(stage, task) and stage == "assemble":
                    failed_assemble.add(rid)

        self.warnings.extend(self.workspace.inspect(set(self.apis)))
        if self.failures:
            self.warnings.append(f"{len(self.failures)} design tasks failed; lowering will use successfully committed records. "
                                 "Requirement coverage is not guaranteed; inspect failed batches.")
        status = "GENERATED_WITH_WARNINGS" if self.failures or self.warnings else "GENERATED"
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
        write_json_atomic(self.traceability_path, {"status": status, "requirements": self.traceability})

    def _requirement_task(self, stage: str, task: dict[str, Any]) -> bool:
        rid = task["id"]
        requirement = {k: v for k, v in task.items() if k != "visual"}
        local = self._requirement_context(rid, {**requirement, "_phase": stage})
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
        instructions = {"ui": UI_INSTRUCTIONS, "assemble": REQUIREMENT_ASSEMBLY_INSTRUCTIONS,
                        "behavior": BEHAVIOR_INSTRUCTIONS}[stage]
        def apply(batch: dict[str, Any]) -> dict[str, Any]:
            mode = batch.get("requirement_mode", "DESIGN")
            if stage == "ui" and mode == "SUMMARY" and (
                task.get("has_visual") or batch["creates"] or batch["updates"] or not context["child_requirements"]
            ):
                raise ValueError("SUMMARY requires a parent without images and empty creates/updates")
            if stage == "ui" and mode == "NONE" and (
                task.get("has_visual") or batch["creates"] or batch["updates"] or batch["associations"]
            ):
                raise ValueError("NONE requires no image and empty creates/updates/associations")
            result = self.workspace.apply_requirement_batch(batch, [rid], set(context["editable_ids"]), stage)
            if stage == "ui":
                previous = self.traceability[rid].get("mode")
                self.traceability[rid]["mode"] = "DESIGN" if task.get("part", 1) > 1 and previous == "DESIGN" else mode
            return result
        applied = self._call(stage, patch_schema(stage), instructions, context, [rid], apply, image=image)
        if applied:
            self._refresh_traceability()
        return applied

    def _requirement_context(self, rid: str, query: Any) -> dict[str, Any]:
        ws = self.workspace
        all_rows = {eid: row for rows in ws.tables.values() for eid, row in rows.items()}
        roots = {c["ui_root_id"] for c in ws.tables["components"].values()}
        linked_ids = {eid for key, values in self.traceability[rid].items() if key.endswith("_ids") for eid in values}
        owned = [row for row in all_rows.values() if row["id"] in linked_ids
                 and row["id"] not in roots and row["id"] not in ws.tables["components"]]
        focus = [row["id"] for row in owned]
        selected = set(focus)
        # Existing matching UI/state is reusable. Retrieval does not schedule extra model calls.
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
                if not focus and field == "children":
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
        # Root/aggregate behavior tasks can otherwise expose the entire
        # application tree (hundreds of UI descendants) and exceed provider
        # context limits. Keep complete records for behavior-bearing entities,
        # component roots and direct use-sites; retain a bounded ranked sample
        # of deep render nodes. This is retrieval compaction, not a semantic
        # validation or model output budget.
        if query.get("_phase") == "behavior" and len(selected) > 180:
            keep: set[str] = set(focus)
            for table in ("components", "data", "properties", "events", "handlers", "effects"):
                keep.update(row["id"] for row in ws.tables[table].values() if row["id"] in selected)
            keep.update(
                row["id"] for row in ws.tables["ui"].values()
                if row["id"] in selected and row.get("kind") in {"COMPONENT", "FRAGMENT"}
            )
            remainder = [all_rows[eid] for eid in selected - keep if eid in all_rows]
            keep.update(row["id"] for row in ranked(remainder, query, max(0, 180 - len(keep))))
            selected = keep
        return {"default_component_id": ws.root_component_id, "focus_ids": focus,
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

    @staticmethod
    def _prepare_context(context: dict[str, Any]) -> dict[str, Any]:
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
        return context

    def _call(self, phase: str, schema: dict[str, Any], instructions: str, context: dict[str, Any],
              requirements: list[str], apply: Callable[[dict[str, Any]], Any],
              image: str | None = None) -> bool:
        original = self._prepare_context(context)
        self.task_count += 1
        task_id = f"{phase}-{self.task_count:05d}"
        feedback = None
        for attempt in range(MODEL_RETRIES + 1):
            payload = copy.deepcopy(original)
            if feedback is not None:
                payload["retry_feedback"] = feedback
            self.log.info(f"MODEL_REQUEST phase=frontend_{phase} task={task_id} "
                          f"requirements={requirements} attempt={attempt + 1}/{MODEL_RETRIES + 1} "
                          f"input_chars={len(encoded(payload))}")
            try:
                kwargs = dict(schema_name=f"frontend_{phase}", instructions=instructions,
                              input_payload=payload, output_schema=schema)
                if image is not None:
                    if self.visual_model is None:
                        self.visual_model = VisualModel.from_env()
                        if hasattr(self.visual_model, "set_usage_path"):
                            self.visual_model.set_usage_path(self.artifact_root / "model_usage.jsonl")
                    raw = self.visual_model.generate_visual_json(**kwargs, image_data_url=image)
                    client = self.visual_model
                else:
                    raw = self.model.generate_json(**kwargs)
                    client = self.model
                output_chars = len(encoded(raw))
                errors = collect_shape_errors(raw, schema)
                errors.extend(check_request_targets(raw, set(self.apis)))
                errors.extend(check_references(
                    raw, {eid for rows in self.workspace.tables.values() for eid in rows},
                    set(original.get("editable_ids", []))))
                if errors:
                    raise BatchValidationError(errors)
                result = apply(normalize_patch(raw))
                # Persist only accepted output and original input, never retry feedback or failures.
                self.batches.append({"task_id": task_id, "phase": phase, "requirement_ids": requirements,
                                     "input": copy.deepcopy(original), "status": "APPLIED",
                                     "output": raw, "merge": result,
                                     "structured_output_mode": getattr(client, "structured_output_mode", "unknown")})
                self.log.info(f"MODEL_APPLIED phase=frontend_{phase} task={task_id} "
                              f"output_chars={output_chars}")
                return True
            except Exception as exc:
                feedback = exc.feedback() if isinstance(exc, ShapeError) else {"error": describe_model_error(exc)}
                feedback["instruction"] = (
                    "The previous response was not merged. Using the unchanged original input and this feedback, "
                    "return a complete replacement batch in the SAME schema, not repair operations.")
                # Error details stay in memory for the next attempt.
                label = "MODEL_RETRY" if attempt < MODEL_RETRIES else "MODEL_SKIPPED"
                self.log.info(f"{label} phase=frontend_{phase} task={task_id}")
        self._fail(phase, requirements, "Retries exhausted; continuing with committed IR.", task_id)
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
