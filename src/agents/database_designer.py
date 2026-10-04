"""Resumable subtree database analysis through plain LLM JSON conversations."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field

from agents.model.factory import create_arc_chat_model
from agents.runtime.runners import parse_json_payload
from core.database_plan import DatabasePlan, Record, ensure_additive, validate_plan
from core.files import read_json_file, write_json_file


class Entity(Record):
    name: str
    purpose: str
    req_ids: list[str]


class Catalog(Record):
    entities: list[Entity]


class DatabaseDelta(DatabasePlan):
    replace_tables: list[str] = Field(default_factory=list)
    replace_seed_tables: list[str] = Field(default_factory=list)


class RejectedRecord(ValueError):
    def __init__(self, reason, payload):
        super().__init__(str(reason))
        self.payload = payload


DATABASE_PROMPT = """Analyze ARC's shared SQLite database before node design and TDD.
You have no tools. Return only JSON matching the supplied schema, never SQL, code or documents.
Use the smallest model justified by requirements. Reuse global entity names and persisted schema;
do not create parallel domain tables. Columns use INTEGER/REAL/TEXT/BLOB/NUMERIC, ASCII identifiers,
literal defaults, explicit non-null primary keys and valid unique/FK constraints.
Every table and seed group cites known requirement IDs. Seed only explicitly pre-existing product
data, never registration/login/order action outputs, screenshots, or invented fixtures. Seeds need
stable identities and unique conflict_columns. No plaintext passwords in hash fields, dynamic
placeholders, or JSON BLOB values. Order seed groups parents before children.

Catalog phase identifies shared entity names and purposes from root and module summaries.
Delta phases return only new or changed tables (complete definitions of those tables), and new
seed groups/rows. Keep untouched records out of the response. Preserve table source IDs and add
newly relevant ones. Empty tables/seeds means no changes. Forward foreign keys may refer to later
batches, but must resolve at final reconciliation. Normally extend earlier draft definitions.
For genuine conflicts explicitly list the touched table in replace_tables or replace_seed_tables,
with corrected complete definitions or all retained seed groups for that table. Never silently
overwrite conflicting values. Replacement is allowed only during resolve/final phases. Never
remove tables or lose requirement coverage. Applied previous_plan is immutable: only additive
tables, safe columns, indexes and seed rows; never redefine or remove applied structures or seeds.
Final phase reconciles references, cross-module naming and seed dependencies. All records must
then validate as a complete database plan. No persistence means empty tables and seeds.
"""


def _nodes(tree):
    yield tree
    for child in tree.get("children", []) or []:
        yield from _nodes(child)


def _summary(node):
    # Full descriptions and scenario steps belong only to the selected subtree.
    return {
        "id": str(node["id"]), "name": node.get("name", ""),
        "type": node.get("type", ""),
        "description_excerpt": str(node.get("description") or "")[:800],
        "dependencies": node.get("dependencies") or [],
        "scenario_names": [item.get("name", "") for item in node.get("scenarios", []) or []],
    }


def _merge(plan, payload, applied, sources, *, resolve=False, final=False):
    delta = DatabaseDelta.model_validate(payload).model_dump()
    replacements = set(delta["replace_tables"])
    seed_replacements = set(delta["replace_seed_tables"])
    if (replacements or seed_replacements) and not resolve:
        raise ValueError("Draft replacements require the explicit resolve phase")
    tables = {table["name"]: deepcopy(table) for table in plan["tables"]}
    incoming = {table["name"]: table for table in delta["tables"]}
    if len(incoming) != len(delta["tables"]):
        raise ValueError("Duplicate table definitions in delta")
    if replacements - (tables.keys() & incoming.keys()) or seed_replacements - tables.keys():
        raise ValueError("Replacement must name an existing draft table and supply its definition")
    for name, table in incoming.items():
        if name in tables:
            if not set(tables[name]["req_ids"]) <= set(table["req_ids"]):
                raise ValueError(f"Cannot lose requirement coverage for {name}")
            if name not in replacements:
                ensure_additive({"tables": [tables[name]], "seeds": []},
                                {"tables": [table], "seeds": []})
        tables[name] = table
    seeds = [deepcopy(seed) for seed in plan["seeds"] if seed["table"] not in seed_replacements]
    for seed in delta["seeds"]:
        if seed not in seeds:
            seeds.append(seed)
    for name in seed_replacements:
        old_ids = {rid for seed in plan["seeds"] if seed["table"] == name for rid in seed["req_ids"]}
        new_ids = {rid for seed in seeds if seed["table"] == name for rid in seed["req_ids"]}
        if not old_ids <= new_ids:
            raise ValueError(f"Cannot lose seed requirement coverage for {name}")
    candidate = validate_plan({"tables": list(tables.values()), "seeds": seeds}, sources,
                              defer_references=not final)
    ensure_additive(applied, candidate)
    return candidate


class DatabaseDesigner:
    def __init__(self, *, workspace_root: str, app_type: str, requirement_path: str, log_cb=None):
        self.workspace_root = workspace_root
        self.app_type = app_type
        self.requirement_path = requirement_path
        self.log_cb = log_cb

    async def _log(self, message):
        if self.log_cb:
            result = self.log_cb("DatabaseDesigner", message, "RUNNING", None)
            if inspect.isawaitable(result):
                await result

    async def _ask(self, model, phase, context, schema, accept):
        messages = [SystemMessage(content=DATABASE_PROMPT), HumanMessage(content=json.dumps({
            "phase": phase, "app_type": self.app_type, "record_schema": schema,
            **context,
        }, ensure_ascii=False))]
        for attempt in range(3):
            response = await model.ainvoke(messages)
            content = response.content
            if not isinstance(content, str):
                content = "\n".join(block if isinstance(block, str) else block.get("text", "")
                                    for block in content if isinstance(block, (str, dict)))
            payload = None
            try:
                payload = parse_json_payload(content)
                if payload is None:
                    raise ValueError("Expected a JSON object")
                return payload, accept(payload)
            except ValueError as exc:
                if attempt == 2:
                    raise RejectedRecord(exc, payload) from exc
                await self._log(f"{phase}: correcting rejected JSON: {exc}")
                messages.extend([response, HumanMessage(content=f"Validation rejected your record: {exc}. "
                                  "Return corrected JSON; the draft has not changed.")])

    async def run(self, requirement_tree: dict[str, Any], baseline: dict[str, Any], *,
                  revision: str = "", requirement_ids: set[str] | None = None) -> dict[str, Any]:
        nodes = list(_nodes(requirement_tree))
        applied = baseline.get("previous_plan") or {"tables": [], "seeds": []}
        sources = set(requirement_ids or ()) | {str(node["id"]) for node in nodes}
        sources.update(rid for item in [*applied["tables"], *applied["seeds"]] for rid in item["req_ids"])
        identity = {"version": 1, "revision": revision, "tree": requirement_tree,
                    "applied": applied, "schema": baseline.get("existing_schema")}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = Path(self.workspace_root) / ".arc/database/analysis.json"
        state = read_json_file(path) or {}
        if state.get("key") != key:
            state = {"key": key, "revision": revision, "plan": deepcopy(applied), "batches": []}
        plan = validate_plan(state["plan"], sources, defer_references=True)
        ensure_additive(applied, plan)
        if state.get("complete") and not baseline.get("previous_failure"):
            return validate_plan(plan, sources)
        model = create_arc_chat_model(os.environ.get("MODEL", "openai:gpt-5.4"))
        if isinstance(model, str):
            from langchain.chat_models import init_chat_model
            model = init_chat_model(model)
        common = {"root": {k: v for k, v in requirement_tree.items() if k != "children"},
                  "module_summaries": [_summary(node) for node in nodes], "baseline": baseline}
        if "catalog" not in state:
            await self._log("Identifying global entities and naming from requirement summaries.")
            def accept_catalog(payload):
                catalog = Catalog.model_validate(payload).model_dump()
                for entity in catalog["entities"]:
                    if not entity["req_ids"] or set(entity["req_ids"]) - sources:
                        raise ValueError("Entity catalog must cite known requirements")
                return catalog
            _, state["catalog"] = await self._ask(model, "catalog", common,
                                                  Catalog.model_json_schema(), accept_catalog)
            write_json_file(path, state)
        # Include root's own requirements; each first-level subtree is visited once.
        batches = [common["root"], *(requirement_tree.get("children", []) or [])]
        for index, batch in enumerate(batches):
            if index < len(state["batches"]):
                continue
            await self._log(f"Analyzing database batch {index + 1}/{len(batches)}: {batch['id']}")
            context = {**common, "catalog": state["catalog"], "requirements": batch, "draft": plan}
            try:
                delta, candidate = await self._ask(model, "subtree", context,
                    DatabaseDelta.model_json_schema(), lambda p: _merge(plan, p, applied, sources))
            except RejectedRecord as exc:
                delta, candidate = await self._ask(model, "resolve", {
                    **context, "conflict": str(exc), "rejected_delta": exc.payload,
                },
                    DatabaseDelta.model_json_schema(),
                    lambda p: _merge(plan, p, applied, sources, resolve=True))
            plan = candidate
            state["plan"] = plan
            state["batches"].append({"root_id": str(batch["id"]), "delta": delta})
            write_json_file(path, state)
        await self._log("Reconciling the accumulated database model across all modules.")
        delta, plan = await self._ask(model, "final", {**common, "catalog": state["catalog"], "draft": plan},
            DatabaseDelta.model_json_schema(),
            lambda p: _merge(plan, p, applied, sources, resolve=True, final=True))
        state.update({"plan": plan, "final_delta": delta, "complete": True})
        write_json_file(path, state)
        return plan
