"""Bounded observation, assembly, UI and behavior passes for the seven-entity IR."""

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

from .design_projection import project_api_contracts
from .frontend_generation_contracts import (
    ASSEMBLY_INSTRUCTIONS, ASSEMBLY_SCHEMA, BEHAVIOR_INSTRUCTIONS,
    OBSERVATION_INSTRUCTIONS, OBSERVATION_SCHEMA, UI_INSTRUCTIONS, check_shape, patch_schema,
)
from .frontend_workspace import FrontendWorkspace, references
from .model_client import StructuredModel, describe_model_error
from .visual_reference import ResolvedVisualReference, VisualModel, VisualStructuredModel


MAX_INPUT_CHARS = 24000
MAX_OUTPUT_CHARS = 20000
MAX_EDITS = 32
TEXT_CHUNK = 3500
CATALOG_SIZE = 12


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def chunks(values: list[Any], size: int):
    for index in range(0, len(values), size):
        yield values[index:index + size]


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


@dataclass
class FrontendIRGenerationResult:
    frontend_ir: dict[str, Any]
    report: dict[str, Any]
    observations: list[dict[str, Any]] = field(default_factory=list)
    batches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.report["status"] in {"GENERATED", "GENERATED_WITH_WARNINGS"}


class FrontendIRGenerationPass:
    def __init__(self, model: StructuredModel, artifact_root: Path,
                 visual_model: VisualStructuredModel | None = None) -> None:
        self.model = model
        self.visual_model = visual_model
        self.log = SynchronousLog("FrontendIRGenerationPass", workspace_root=artifact_root.resolve().parent)

    def compile(self, requirement_ir: dict[str, Any], dependency_graph: dict[str, Any],
                backend_design_ir: dict[str, Any],
                visual_references: list[ResolvedVisualReference | dict[str, Any]]) -> FrontendIRGenerationResult:
        self.workspace = FrontendWorkspace()
        self.observations: dict[str, dict[str, Any]] = {}
        self.batches: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.nodes = requirement_ir.get("nodes", {})
        self.dependencies = dependency_graph.get("requirement_dependencies", {})
        self.apis = {row["id"]: row for row in project_api_contracts(backend_design_ir)}
        self.api_owners: dict[str, set[str]] = {}
        for row in backend_design_ir.get("requirements", []):
            self.api_owners[str(row.get("id", ""))] = set(row.get("api_ids", []))
        for row in backend_design_ir.get("modules", []):
            if row.get("kind") == "API":
                self.api_owners.setdefault(str(row.get("owner_requirement", "")), set()).add(row["id"])
        ordered = list(dict.fromkeys([*requirement_ir.get("node_order", []), *self.nodes]))
        root_id = requirement_ir.get("root_id")
        if ordered:
            self.workspace.ensure_root([root_id] if root_id in self.nodes else [ordered[0]])

        # S1: visit every requirement, splitting long original text instead of dropping its tail.
        for rid in ordered:
            node = self.nodes[rid]
            source = str(node.get("description", "")) + "\n" + encoded(node.get("scenarios", []))
            fragments = [source[i:i + TEXT_CHUNK] for i in range(0, len(source), TEXT_CHUNK)] or [""]
            for index, fragment in enumerate(fragments):
                context = {"requirement": {"id": rid, "name": node.get("name", ""),
                                           "fragment": fragment, "part": index + 1, "parts": len(fragments)},
                           "related_requirements": self._requirement_summaries([rid]),
                           "api_catalog": self._api_context([rid], fragment, full=False),
                           "existing_observations": self._observation_catalog(fragment)}
                self._call("observe", OBSERVATION_SCHEMA, OBSERVATION_INSTRUCTIONS, context, [rid],
                           lambda batch, ids=[rid]: self._merge_observations(batch, ids, []))
        for visual in visual_references:
            self._observe_visual(visual)

        # S2: small slices of observations; App and earlier component identities remain compiler-owned.
        for group in chunks(list(self.observations.values()), 3):
            rids = list(dict.fromkeys(r for obs in group for r in obs["requirement_ids"]))
            context = {"observations": group, "root_component_id": self.workspace.root_component_id,
                       "components": self._component_catalog(group)}
            self._call("assemble", ASSEMBLY_SCHEMA, ASSEMBLY_INSTRUCTIONS, context, rids,
                       lambda batch, ids=rids, obs={o["id"] for o in group}:
                       self.workspace.apply_assembly(batch, ids, obs))
        self.workspace.add_regions(self.observations)

        # S3: each region gets its own UI/data task, then a small assembly/layout task per component.
        failed_components: set[str] = set()
        for cid in self.workspace.parent_first_components():
            for oid, uid in self.workspace.regions.get(cid, {}).items():
                observation = self.observations[oid]
                if not self._patch_task("ui", cid, [uid], {"observation": observation},
                                        observation["requirement_ids"]):
                    failed_components.add(cid)
            component = self.workspace.tables["components"][cid]
            root = component["ui_root_id"]
            if not self._patch_task("ui", cid, [root], {"task": "Arrange the component assembly skeleton."},
                                    component["requirement_ids"], skeleton=True):
                failed_components.add(cid)

        # S4: bounded behavior batches; an empty list still asks about mount/cleanup behavior.
        for cid in self.workspace.parent_first_components():
            if cid in failed_components:
                self._fail("behavior", self.workspace.tables["components"][cid]["requirement_ids"],
                           "UI task failed; dependent behavior tasks were not run.")
                continue
            regions = self.workspace.regions.get(cid, {})
            if not regions:
                component = self.workspace.tables["components"][cid]
                self._patch_task("behavior", cid, [component["ui_root_id"]],
                                 {"behavior_hints": [], "task": "Component lifecycle behavior, if needed."},
                                 component["requirement_ids"], skeleton=True)
            for oid, uid in regions.items():
                observation = self.observations[oid]
                for hints in chunks(observation["behavior_hints"], 3) if observation["behavior_hints"] else [[]]:
                    self._patch_task("behavior", cid, [uid],
                                     {"observation": {k: v for k, v in observation.items() if k != "behavior_hints"},
                                      "behavior_hints": hints}, observation["requirement_ids"])
        # Child CUSTOM events are known only now. Wire one parent use-site per call, without redoing S3.
        for use in list(self.workspace.tables["ui"].values()):
            if use["kind"] != "COMPONENT" or use["component_id"] in failed_components:
                continue
            events = [e for e in self.workspace.tables["events"].values()
                      if e["component_id"] == use["component_ref"] and e["kind"] == "CUSTOM"]
            for event_group in chunks(events, 3) if events else [[]]:
                self._patch_task("behavior", use["component_id"], [use["id"]],
                                 {"task": "Complete child input arguments and wire callbacks required by the requirements.",
                                  "child_events": event_group}, use["requirement_ids"], skeleton=True)

        self.warnings.extend(self.workspace.inspect(set(self.apis)))
        if self.observations and not self.workspace.observation_components:
            self.warnings.append("No observations were assigned to components.")
        status = "PARTIAL" if self.failures else ("GENERATED_WITH_WARNINGS" if self.warnings else "GENERATED")
        if not self.nodes:
            status = "FAILED"
            self.warnings.append("No requirements were supplied.")
        report = {"status": status, "warnings": list(dict.fromkeys(self.warnings)),
                  "failed_tasks": self.failures,
                  "task_counts": {phase: sum(b["phase"] == phase for b in self.batches)
                                  for phase in ("observe", "visual", "assemble", "ui", "behavior")},
                  "observation_components": self.workspace.observation_components}
        return FrontendIRGenerationResult(self.workspace.export(), report,
                                          list(self.observations.values()), self.batches)

    def _call(self, phase: str, schema: dict[str, Any], instructions: str, context: dict[str, Any],
              requirements: list[str], apply: Callable[[dict[str, Any]], Any],
              image: str | None = None) -> bool:
        task_id = f"{phase}-{len(self.batches) + 1:05d}"
        record: dict[str, Any] = {"task_id": task_id, "phase": phase,
                                  "requirement_ids": requirements, "input": copy.deepcopy(context), "attempts": []}
        self.batches.append(record)
        if len(encoded(context)) > MAX_INPUT_CHARS:
            self._fail(phase, requirements, "Local context exceeds the input budget; task retained as incomplete.", task_id)
            record["status"] = "FAILED"
            return False
        feedback = ""
        for attempt in range(2):
            payload = copy.deepcopy(context)
            if feedback:
                payload["repair_feedback"] = feedback[:900]
            self.log.info(f"MODEL_REQUEST phase=frontend_{phase} task={task_id} attempt={attempt + 1}/2 input_chars={len(encoded(payload))}")
            raw = None
            try:
                kwargs = dict(schema_name=f"frontend_{phase}", instructions=instructions,
                              input_payload=payload, output_schema=schema)
                if image is not None:
                    if self.visual_model is None:
                        self.visual_model = VisualModel.from_env()
                    raw = self.visual_model.generate_visual_json(**kwargs, image_data_url=image)
                else:
                    raw = self.model.generate_json(**kwargs)
                if len(encoded(raw)) > MAX_OUTPUT_CHARS:
                    raise ValueError("Output too large; return concise regional edits, not the whole component.")
                check_shape(raw, schema)
                if len(raw.get("creates", [])) + len(raw.get("updates", [])) > MAX_EDITS:
                    raise ValueError(f"Return at most {MAX_EDITS} local edits for this task.")
                result = apply(raw)
                record["attempts"].append({"output": raw, "status": "APPLIED", "merge": result})
                record["status"] = "APPLIED"
                self.log.info(f"MODEL_APPLIED phase=frontend_{phase} task={task_id} output_chars={len(encoded(raw))}")
                return True
            except Exception as exc:
                feedback = describe_model_error(exc)
                record["attempts"].append({"output": raw, "error": feedback})
                self.log.info(f"MODEL_RETRY phase=frontend_{phase} task={task_id} error={feedback}")
        record["status"] = "FAILED"
        self._fail(phase, requirements, feedback, task_id)
        return False

    def _fail(self, phase: str, requirements: list[str], message: str, task_id: str = "") -> None:
        self.failures.append({"task_id": task_id, "phase": phase,
                              "requirement_ids": requirements, "message": message})

    def _merge_observations(self, batch: dict[str, Any], requirements: list[str], visuals: list[str]) -> None:
        candidate = copy.deepcopy(self.observations)
        for item in batch["observations"]:
            oid = item["existing_observation_id"]
            if oid and oid not in candidate:
                raise ValueError(f"Unknown observation {oid}")
            if not item["name"].strip() or not item["spec"].strip():
                raise ValueError("Observation name and spec must be nonempty")
            if not oid:
                oid = f"O.{len(candidate) + 1:06d}"
                candidate[oid] = {"id": oid, "requirement_ids": [], "visual_reference_ids": [],
                                  "content": [], "behavior_hints": [], "structure_hint": "", "spec": ""}
            row = candidate[oid]
            row["name"] = item["name"]
            for key in ("spec", "structure_hint"):
                if item[key] and item[key] not in row[key]:
                    row[key] = (row[key] + "\n" + item[key]).strip()
            for key, values in (("content", item["content"]), ("behavior_hints", item["behavior_hints"]),
                                ("requirement_ids", requirements), ("visual_reference_ids", visuals)):
                row[key].extend(value for value in values if value not in row[key])
        self.observations = candidate

    def _observe_visual(self, visual: ResolvedVisualReference | dict[str, Any]) -> None:
        if isinstance(visual, dict):
            rids = [rid for rid in visual.get("requirement_ids", []) if rid in self.nodes]
            context = {"visual_analysis": visual.get("analysis", {}),
                       "related_requirements": self._requirement_summaries(rids),
                       "existing_observations": self._observation_catalog(visual.get("analysis", {}))}
            self._call("visual", OBSERVATION_SCHEMA, OBSERVATION_INSTRUCTIONS, context, rids,
                       lambda batch: self._merge_observations(batch, rids, [str(visual.get("id", ""))]))
            self.warnings.append(f"Visual {visual.get('id')}: used supplied analysis, not raw pixels.")
            return
        rids = [rid for rid in visual.requirement_ids if rid in self.nodes]
        try:
            content = visual.absolute_path.read_bytes()
            if hashlib.sha256(content).hexdigest() != visual.sha256:
                raise ValueError("Visual reference changed after resolution")
            image = f"data:{visual.media_type};base64,{base64.b64encode(content).decode('ascii')}"
        except (OSError, ValueError) as exc:
            self.warnings.append(f"Visual {visual.id} unavailable: {exc}")
            return
        context = {"visual_reference_id": visual.id, "related_requirements": self._requirement_summaries(rids),
                   "existing_observations": self._observation_catalog(rids)}
        self._call("visual", OBSERVATION_SCHEMA, OBSERVATION_INSTRUCTIONS, context, rids,
                   lambda batch: self._merge_observations(batch, rids, [visual.id]), image=image)

    def _observation_catalog(self, query: Any) -> list[dict[str, Any]]:
        return [brief(row) for row in ranked(list(self.observations.values()), query, CATALOG_SIZE)]

    def _component_catalog(self, query: Any) -> list[dict[str, Any]]:
        selected = ranked(list(self.workspace.tables["components"].values()), query, CATALOG_SIZE)
        root = self.workspace.tables["components"].get(self.workspace.root_component_id)
        if root and root not in selected:
            selected.insert(0, root)
        return [{**brief(row), "inputs": [brief(d) | {"type": d["type"]} for d in self.workspace.tables["data"].values()
                                         if d["owner_id"] == row["id"]],
                 "properties": [brief(p) | {"type": p["type"]} for p in self.workspace.tables["properties"].values()
                                if p["component_id"] == row["id"]],
                 "uses": [brief(u) for u in self.workspace.tables["ui"].values()
                          if u["component_id"] == row["id"] and u["kind"] == "COMPONENT"]} for row in selected]

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

    def _patch_task(self, stage: str, cid: str, focus_ids: list[str], task: dict[str, Any],
                    requirements: list[str], skeleton: bool = False) -> bool:
        local = self._local_context(cid, focus_ids, task, skeleton)
        context = {**task, **local, "requirements": self._requirement_summaries(requirements),
                   "api_contracts": self._api_context(requirements, task)}
        # A hint may explicitly name an API outside the ranked shortlist; include it intact.
        wanted = {aid for hint in task.get("behavior_hints", []) for aid in hint.get("api_ids", [])}
        current = {row["id"] for row in context["api_contracts"]}
        context["api_contracts"].extend(copy.deepcopy(self.apis[aid]) for aid in sorted(wanted - current) if aid in self.apis)
        schema = patch_schema(stage)
        def apply(batch: dict[str, Any]) -> dict[str, Any]:
            before = {edit["id"]: copy.deepcopy(self.workspace.find(edit["id"])[1]) for edit in batch["updates"]}
            aliases = self.workspace.apply_patch(batch, cid, requirements, set(local["editable_ids"]), stage)
            return {"aliases": aliases, "changes": [
                {"before": row, "after": copy.deepcopy(self.workspace.find(entity_id)[1])}
                for entity_id, row in before.items()
            ]}
        return self._call(stage, schema, UI_INSTRUCTIONS if stage == "ui" else BEHAVIOR_INSTRUCTIONS,
                          context, requirements, apply)

    def _local_context(self, cid: str, focus_ids: list[str], query: Any, skeleton: bool) -> dict[str, Any]:
        ws = self.workspace
        selected = set(focus_ids)
        pending = list(focus_ids)
        while pending:
            uid = pending.pop()
            ui = ws.tables["ui"].get(uid)
            if not ui:
                continue
            for child in ui["children"]:
                if child not in selected:
                    selected.add(child)
                    if not skeleton:
                        pending.append(child)
        owned = [row for rows in ws.tables.values() for row in rows.values() if ws.owner(row["id"]) == cid]
        owned_ids = {row["id"] for row in owned}
        shared = [row for row in owned if row["id"].startswith(("D.", "P."))]
        selected.update(row["id"] for row in ranked(shared, query, 16))
        for row in owned:
            if row["id"].startswith("E.") and row.get("ui_id") in selected:
                selected.add(row["id"])
        # Follow local data/behavior references; don't expand a skeleton into every UI descendant.
        changed = True
        while changed:
            before = len(selected)
            for row in owned:
                if row["id"] in selected:
                    selected.update(target for key, target in references(row)
                                    if key not in {"children", "component_ref", "component_id", "ui_root_id"}
                                    and target in owned_ids)
                if row.get("owner_id") in selected:
                    selected.add(row["id"])
            changed = len(selected) != before
        current_rows = [copy.deepcopy(row) for row in owned if row["id"] in selected]
        contracts = []
        child_ids = {row.get("component_ref") for row in current_rows if row.get("kind") == "COMPONENT"}
        for child in child_ids:
            contracts.extend(copy.deepcopy(row) for row in ws.tables["data"].values() if row["owner_id"] == child)
            events = [row for row in ws.tables["events"].values() if row["component_id"] == child and row["kind"] == "CUSTOM"]
            contracts.extend(copy.deepcopy(events))
            contracts.extend(copy.deepcopy(row) for row in ws.tables["data"].values()
                             if row["owner_id"] in {e["id"] for e in events})
        # Let child tasks see their parent-provided input values without granting parent edit authority.
        parent_uses = [copy.deepcopy(u) for u in ws.tables["ui"].values()
                       if u["kind"] == "COMPONENT" and u["component_ref"] == cid]
        return {"component": copy.deepcopy(ws.tables["components"][cid]), "focus_ids": focus_ids,
                "entities": current_rows, "editable_ids": sorted(row["id"] for row in current_rows),
                "related_contracts": contracts, "parent_uses": parent_uses,
                "other_local_entities": [brief(row) for row in ranked(
                    [r for r in owned if r["id"] not in selected], query, 12)]}


def frontend_ir_traceability(frontend_ir: dict[str, Any]) -> dict[str, dict[str, Any]]:
    links: dict[str, dict[str, Any]] = {}
    for table, field_name in (("components", "component_ids"), ("ui", "ui_ids"),
                              ("data", "data_ids"), ("properties", "property_ids"),
                              ("events", "event_ids"), ("handlers", "handler_ids"), ("effects", "effect_ids")):
        for row in frontend_ir.get(table, []):
            for rid in row["requirement_ids"]:
                link = links.setdefault(rid, {"ui_scope": "UI_REQUIRED"})
                link.setdefault(field_name, []).append(row["id"])
    return links
