"""Resumable subtree database analysis through plain LLM JSON conversations."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from agents.model.factory import create_arc_chat_model
from agents.model.native_openai import generate_text
from core.database_plan import DatabasePlan, Record, Table, Seed, identifier, ensure_additive, validate_plan
from core.files import read_json_file, write_json_file


class Entity(Record):
    name: str
    purpose: str
    req_ids: list[str]
    identity: str
    identity_keys: list[str]
    aliases: list[str]
    roles: list[str]


class Catalog(Record):
    entities: list[Entity]


class Evidence(Record):
    req_id: str
    quote: str = Field(min_length=1)


class PersistenceFact(Record):
    kind: Literal["entity", "identity_key", "relationship", "state", "seed", "no_persistence"]
    entity: str
    target: str = ""
    detail: str = Field(min_length=1, max_length=3000)
    evidence: list[Evidence] = Field(min_length=1)


class Facts(Record):
    facts: list[PersistenceFact]


class Binding(Record):
    candidate: str
    canonical: str
    reason: str = Field(min_length=1)


class IdentityResolution(Catalog):
    bindings: list[Binding]


class SchemaDelta(Record):
    tables: list[Table]
    replace_tables: list[str] = Field(default_factory=list)


class SeedDelta(Record):
    seeds: list[Seed]


class DatabaseDelta(DatabasePlan):
    replace_tables: list[str] = Field(default_factory=list)
    replace_seed_tables: list[str] = Field(default_factory=list)


class RejectedRecord(ValueError):
    def __init__(self, reason, payload):
        super().__init__(str(reason))
        self.payload = payload


DATABASE_PROMPT = """Analyze ARC's shared SQLite database before node design and TDD.
Include persistent identity/session prerequisites implied by requirements. Shared
application modules are discovered later; database analysis does not design their APIs.
Return only a JSON array of the supplied tool calls, never SQL, code, documents,
explanations or wrapper properties. Parameters are directly on each call.
Use the smallest model justified by requirements. Reuse global entity names and persisted schema;
do not create parallel domain tables. Columns use INTEGER/REAL/TEXT/BLOB/NUMERIC, ASCII identifiers,
literal defaults, explicit non-null primary keys and valid unique/FK constraints.
Every table and seed group cites known requirement IDs. Seed only explicitly pre-existing product
data, never registration/login/order action outputs, screenshots, or invented fixtures. Seeds need
stable identities and unique conflict_columns. No plaintext passwords in hash fields, dynamic
placeholders, or JSON BLOB values. Order seed groups parents before children.

Facts phase uses record_persistence_fact: extract entity candidates, identity keys,
relationships (cardinality/ownership/deletion), persisted states and explicit seed
obligations BEFORE defining tables. Cite exact text quotes from the supplied requirement
node including scenarios. State inferred prerequisites explicitly in detail; do not
pretend an inference was written in the source. Every node must have evidence in a
fact or a no_persistence fact. Candidate names are local concepts, not final tables.
Use qualified local candidate names when identical words describe different identities.
Seed facts describe the required pre-existing data and its source, not a copy of a
large row dataset; the seed phase receives the original controlled-size subtree.
Identity phase uses define_entity for NEW canonical identities and bind_entity for
EVERY candidate (including relationship targets). Match identity/lifecycle/keys and
roles, not spelling. Login user and passenger may share an identity only when evidence
supports this; a passenger may exist without an account. Roles are not automatically
entities, and equal names do not prove equal identities. Existing canonical identities
are immutable. Bind to an existing identity or define a distinct one with a clear reason.
Canonical entity names are ASCII SQL table names. Existing persisted tables retain names.
Schema phases receive normalized facts, a lightweight global entity/table index and
only selected tables plus direct FK neighbours. Use the existing primary table name
for each canonical identity; supplemental/association tables must implement an explicit
fact. Keep out unrelated tables. Seed rows are generated in a separate seeds phase;
never return seed_rows during schema phases. Seeds phase receives only seed facts and
their relevant schema, plus seed summaries without accumulated row payloads.
Delta phases use define_table only for new or changed tables (complete definitions), and
seed_rows for new seed groups/rows. Keep untouched records out of the response. Preserve table source IDs and add
newly relevant ones. [] means no changes. Forward foreign keys may refer to later
batches, but must resolve at final reconciliation. Normally extend earlier draft definitions.
For genuine conflicts call replace_table(name) or replace_seed_rows(name),
plus define_table or seed_rows with corrected complete definitions or all retained seed groups. Never silently
overwrite conflicting values. Replacement is allowed only during resolve/reconcile phases. Never
remove tables or lose requirement coverage. Applied previous_plan is immutable: only additive
tables, safe columns, indexes and seed rows; never redefine or remove applied structures or seeds.
Reconcile phases revisit one fact batch against the accumulated RELATED schema.
Final structural validation is deterministic, not a giant all-schema model prompt. Records must
then validate as a complete database plan. No persistence means [].
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


def _entity_index(catalog):
    return [{"name": entity["name"], "identity": entity["identity"][:320],
             "identity_keys": entity["identity_keys"][:16], "aliases": entity["aliases"][:20],
             "roles": entity["roles"][:20]} for entity in catalog]


def _schema_context(plan, facts, bindings, req_ids):
    names = {binding["canonical"] for binding in bindings}
    tables = {table["name"]: table for table in plan["tables"]}
    selected = names & tables.keys()
    selected.update(table["name"] for table in tables.values() if set(table["req_ids"]) & req_ids)
    # Include direct incoming and outgoing neighbours, not the whole FK closure.
    primary = set(selected)
    for table in tables.values():
        targets = {column["references"]["table"] for column in table["columns"] if column["references"]}
        if table["name"] in primary:
            selected.update(targets & tables.keys())
        if targets & primary:
            selected.add(table["name"])
    return {"facts": facts, "bindings": bindings,
            "schema_index": [{"name": table["name"], "keys": [table["primary_key"], *table["unique"]],
                              "references": sorted({col["references"]["table"] for col in table["columns"] if col["references"]})}
                             for table in tables.values()],
            "draft": {"tables": [tables[name] for name in sorted(selected)], "seeds": []}}


def _order_seed_dependencies(plan):
    """Order across batches as well as within them; preserve row order per table."""
    grouped = {}
    for seed in plan["seeds"]:
        grouped.setdefault(seed["table"], []).append(seed)
    dependencies = {table["name"]: {column["references"]["table"] for column in table["columns"]
                                    if column["references"] and column["references"]["table"] != table["name"]}
                    for table in plan["tables"]}
    ordered = []
    remaining = set(grouped)
    while remaining:
        ready = sorted(name for name in remaining if not dependencies.get(name, set()) & remaining)
        if not ready:
            raise ValueError("Cyclic cross-table seed dependencies require explicit handling: " + ", ".join(sorted(remaining)))
        for name in ready:
            ordered.extend(grouped[name])
            remaining.remove(name)
    plan["seeds"] = ordered
    return plan


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


def _merge_related(plan, payload, applied, sources, context, *, resolve=False):
    delta = SchemaDelta.model_validate(payload).model_dump()
    seen = {table["name"] for table in context["draft"]["tables"]}
    all_names = {table["name"] for table in plan["tables"]}
    if any(table["name"] in all_names - seen for table in delta["tables"]):
        raise ValueError("Cannot redefine an unrelated table whose complete definition was not supplied")
    return _merge(plan, {**delta, "seeds": []}, applied, sources, resolve=resolve)


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
        from agents.model.prompt_input import format_task_input
        from agents.model.tool_sequence import parse_tool_sequence, tool_contract
        initial = [{"role": "system", "content": DATABASE_PROMPT}, {"role": "user", "content": format_task_input({
            "phase": phase, "app_type": self.app_type, **context,
        }, tool_contract(schema))}]
        messages = initial
        for attempt in range(3):
            content = await generate_text(model, messages, stage=f"DATABASE_PREPARE_{phase}",
                                          workspace_root=self.workspace_root)
            payload = None
            try:
                # Only harmless wrappers: never extract arbitrary JSON from prose.
                normalized = content.strip()
                fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", normalized, re.DOTALL)
                if fence:
                    normalized = fence[1].strip()
                    await self._log(f"{phase}: normalized a JSON code fence without changing records.")
                payload = parse_tool_sequence(normalized, schema)
                return payload, accept(payload)
            except ValueError as exc:
                if attempt == 2:
                    raise RejectedRecord(exc, payload) from exc
                await self._log(f"{phase}: correcting rejected JSON: {exc}")
                # Keep the task and latest candidate, not every rejected conversation.
                messages = [*initial, {"role": "assistant", "content": content},
                                 {"role": "user", "content": f"Validation rejected your record: {exc}. "
                                  "Return only the complete corrected tool-call array; preserve valid records. "
                                  "The draft has not changed. Never remove source coverage or weaken applied structures to pass validation."}]

    async def run(self, requirement_tree: dict[str, Any], baseline: dict[str, Any], *,
                  revision: str = "", requirement_ids: set[str] | None = None) -> dict[str, Any]:
        nodes = list(_nodes(requirement_tree))
        applied = baseline.get("previous_plan") or {"tables": [], "seeds": []}
        sources = set(requirement_ids or ()) | {str(node["id"]) for node in nodes}
        sources.update(rid for item in [*applied["tables"], *applied["seeds"]] for rid in item["req_ids"])
        identity = {"version": 2, "revision": revision, "tree": requirement_tree,
                    "applied": applied, "schema": baseline.get("existing_schema")}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = Path(self.workspace_root) / ".arc/database/analysis.json"
        seeds_path = path.with_name("seeds.json")
        state = read_json_file(path) or {}
        if state.get("key") != key:
            old_catalog = state.get("catalog", [])
            if isinstance(old_catalog, dict):
                old_catalog = old_catalog.get("entities", [])
            old_identities = {entity["name"]: entity for entity in old_catalog if "identity" in entity}
            state = {"key": key, "revision": revision, "plan": {"tables": deepcopy(applied["tables"])},
                     "fact_batches": [], "identity_batches": [], "batches": [], "reconciled": [],
                     "catalog": [old_identities.get(table["name"]) or {"name": table["name"], "purpose": "Existing persistent entity",
                                  "identity": "Existing table: " + table["name"],
                                  "identity_keys": [",".join(table["primary_key"])], "aliases": [],
                                  "roles": [], "req_ids": table["req_ids"]} for table in applied["tables"]]}
            write_json_file(seeds_path, {"key": key, "seeds": deepcopy(applied["seeds"])})
            write_json_file(path, state)
        seed_state = read_json_file(seeds_path) or {}
        if seed_state.get("key") != key:
            raise ValueError("Database seed intermediate records are missing or belong to another analysis revision")
        state["plan"]["seeds"] = seed_state["seeds"]
        plan = validate_plan(state["plan"], sources, defer_references=True)
        ensure_additive(applied, plan)
        if state.get("complete") and not baseline.get("previous_failure"):
            return validate_plan(plan, sources)
        model = create_arc_chat_model(os.environ.get("MODEL", "openai:gpt-5.4"))
        # Row payloads stay in a separate keyed record, never in analysis prompts/history.
        def persist():
            write_json_file(seeds_path, {"key": key, "seeds": plan["seeds"]})
            state["plan"] = {"tables": plan["tables"]}
            write_json_file(path, state)

        root_context = _summary(requirement_tree)
        batches = [{k: v for k, v in requirement_tree.items() if k != "children"},
                   *(requirement_tree.get("children", []) or [])]
        # Extract every controlled-size subtree before committing any new schema.
        for index, batch in enumerate(batches):
            if index < len(state["fact_batches"]):
                continue
            await self._log(f"Extracting persistence facts {index + 1}/{len(batches)}: {batch['id']}")
            batch_nodes = {str(node["id"]): node for node in _nodes(batch)}
            def accept_facts(payload):
                facts = Facts.model_validate(payload).model_dump()["facts"]
                covered = set()
                for fact in facts:
                    if fact["kind"] != "no_persistence" and not fact["entity"].strip():
                        raise ValueError("Persistent facts require an entity candidate")
                    if fact["kind"] == "relationship" and not fact["target"].strip():
                        raise ValueError("Relationship facts require a target candidate")
                    for evidence in fact["evidence"]:
                        node = batch_nodes.get(evidence["req_id"])
                        if node is None:
                            raise ValueError(f"Fact {fact['entity']} cites invalid req_id {evidence['req_id']}; allowed IDs: {sorted(batch_nodes)}. Cite this batch's text only.")
                        text = "\n".join([str(node.get("name", "")), str(node.get("description", "")),
                                         json.dumps(node.get("scenarios", []), ensure_ascii=False)])
                        if evidence["quote"] not in text:
                            # Scenario content can contain line breaks escaped in JSON.
                            scenario_text = "\n".join(str(step.get("content", "")) for scenario in node.get("scenarios", []) or []
                                                       for step in scenario.get("steps", []) or [])
                            if evidence["quote"] not in scenario_text:
                                normalize = lambda value: " ".join(value.split())
                                if normalize(evidence["quote"]) not in normalize(text + "\n" + scenario_text):
                                    raise ValueError(f"Fact {fact['entity']} has a non-verbatim quote for {evidence['req_id']}: {evidence['quote'][:180]!r}. Copy an actual contiguous source phrase; whitespace differences alone are tolerated.")
                        covered.add(evidence["req_id"])
                if set(batch_nodes) - covered:
                    raise ValueError("Missing persistence/no_persistence evidence for: " + ", ".join(sorted(set(batch_nodes) - covered)))
                return facts
            _, facts = await self._ask(model, "facts", {"root_summary": root_context, "requirements": batch},
                                       Facts.model_json_schema(), accept_facts)
            state["fact_batches"].append({"root_id": str(batch["id"]), "facts": facts})
            persist()

        # Resolve local candidate identity against the slim canonical index.
        for index, record in enumerate(state["fact_batches"]):
            if index < len(state["identity_batches"]):
                continue
            facts = record["facts"]
            candidates = {fact["entity"] for fact in facts if fact["kind"] != "no_persistence"}
            candidates.update(fact["target"] for fact in facts if fact["kind"] == "relationship")
            if not candidates:
                state["identity_batches"].append({"root_id": record["root_id"], "bindings": []})
                persist()
                continue
            batch_ids = {item["req_id"] for fact in facts for item in fact["evidence"]}
            partial = {"entities": [], "bindings": []}
            rescuing = False
            def accept_identity(payload):
                resolved = IdentityResolution.model_validate(payload).model_dump()
                # During targeted rescue retain only validated mappings from the last
                # response; current entries override old entries explicitly.
                if rescuing:
                    retained = {entity["name"]: entity for entity in partial["entities"]}
                    for entity in resolved["entities"]:
                        if entity["name"] in retained and entity != retained[entity["name"]]:
                            raise ValueError(f"Rescue must preserve accepted entity {entity['name']}")
                        retained[entity["name"]] = entity
                    resolved["entities"] = list(retained.values())
                    resolved["bindings"] = partial["bindings"] + resolved["bindings"]
                existing = {entity["name"] for entity in state["catalog"]}
                additions = resolved["entities"]
                names = [entity["name"] for entity in additions]
                if len({name.casefold() for name in names}) != len(names) or {name.casefold() for name in existing} & {name.casefold() for name in names}:
                    raise ValueError("Define only NEW canonical entities; bind existing identities instead")
                for entity in additions:
                    identifier(entity["name"])
                    if not entity["identity"].strip() or not entity["req_ids"] or set(entity["req_ids"]) - batch_ids:
                        raise ValueError("Canonical identity requires a description and current batch source IDs")
                # The compiler owns the candidate inventory. Discard invented entries
                # and identical duplicates; never guess a missing semantic mapping.
                by_candidate = {}
                for binding in resolved["bindings"]:
                    candidate = binding["candidate"]
                    if candidate not in candidates:
                        continue
                    prior = by_candidate.get(candidate)
                    if prior and prior["canonical"] != binding["canonical"]:
                        raise ValueError(f"Conflicting binding for {candidate}: {prior['canonical']} versus {binding['canonical']}; choose exactly one identity")
                    by_candidate.setdefault(candidate, binding)
                missing = sorted(candidates - by_candidate.keys())
                valid = [binding for binding in by_candidate.values() if binding["canonical"] in existing | set(names)]
                partial["bindings"] = valid
                partial["entities"] = [entity for entity in additions
                                       if entity["name"] in {binding["canonical"] for binding in valid}]
                if missing:
                    raise ValueError("Missing entity bindings: " + json.dumps(missing, ensure_ascii=False)
                                     + ". Return the complete corrected batch, preserving valid entities/bindings. "
                                     + "State/consent/transaction candidates may bind to their owning entity; they do not each require a new table.")
                bindings = [by_candidate[name] for name in sorted(candidates)]
                resolved["bindings"] = bindings
                if any(item["canonical"] not in existing | set(names) for item in bindings):
                    raise ValueError("Binding refers to an unknown canonical identity")
                # Definitions used only by discarded, invented candidates are inert.
                resolved["entities"] = [entity for entity in additions
                                        if entity["name"] in {item["canonical"] for item in bindings}]
                return resolved
            await self._log(f"Resolving entity identities {index + 1}/{len(batches)}: {record['root_id']}")
            identity_context = {"facts": facts,
                                          "required_candidates": sorted(candidates),
                                          "binding_rule": "Bind exactly these candidate strings. Do not add candidates from no_persistence facts. Facts describing a state, consent or transaction can map to their owning canonical entity rather than inventing an entity/table.",
                                          "entity_index": _entity_index(state["catalog"])}
            try:
                _, resolved = await self._ask(model, "identity", identity_context,
                                              IdentityResolution.model_json_schema(), accept_identity)
            except RejectedRecord as exc:
                missing = sorted(candidates - {binding["candidate"] for binding in partial["bindings"]})
                if not missing:
                    raise
                await self._log(f"Identity rescue: preserving {len(partial['bindings'])} valid bindings; requesting only {missing}.")
                rescuing = True
                _, resolved = await self._ask(model, "identity_rescue", {
                    **identity_context, "accepted_partial": deepcopy(partial), "unresolved_candidates": missing,
                    "previous_error": str(exc),
                    "task": "Return only missing bind_entity calls and any NEW entities they require. Accepted partial bindings are merged by the system. Do not redefine or rebind accepted identities.",
                }, IdentityResolution.model_json_schema(), accept_identity)
            state["catalog"].extend(resolved["entities"])
            for binding in resolved["bindings"]:
                entity = next(item for item in state["catalog"] if item["name"] == binding["canonical"])
                entity["aliases"] = sorted(set(entity["aliases"]) | {binding["candidate"]})
                related_ids = {evidence["req_id"] for fact in facts
                               if binding["candidate"] in {fact["entity"], fact["target"]}
                               for evidence in fact["evidence"]}
                entity["req_ids"] = sorted(set(entity["req_ids"]) | related_ids)
            state["identity_batches"].append({"root_id": record["root_id"], "bindings": resolved["bindings"]})
            persist()

        for index, batch in enumerate(batches):
            if index < len(state["batches"]):
                continue
            await self._log(f"Analyzing database batch {index + 1}/{len(batches)}: {batch['id']}")
            facts = state["fact_batches"][index]["facts"]
            bindings = state["identity_batches"][index]["bindings"]
            if not bindings:
                state["batches"].append({"root_id": str(batch["id"]), "delta": {"tables": []}})
                persist()
                continue
            batch_ids = {str(node["id"]) for node in _nodes(batch)}
            context = {"root_summary": root_context, "entity_index": _entity_index(state["catalog"]),
                       "requirements": batch, **_schema_context(plan, facts, bindings, batch_ids)}
            context["existing_schema"] = {name: definition for name, definition in (baseline.get("existing_schema") or {}).items()
                                          if name in {binding["canonical"] for binding in bindings}
                                          or name in {table["name"] for table in context["draft"]["tables"]}}
            try:
                delta, candidate = await self._ask(model, "subtree", context,
                    SchemaDelta.model_json_schema(), lambda p: _merge_related(plan, p, applied, sources, context))
            except RejectedRecord as exc:
                delta, candidate = await self._ask(model, "resolve", {
                    **context, "conflict": str(exc), "rejected_delta": exc.payload,
                },
                    SchemaDelta.model_json_schema(), lambda p: _merge_related(plan, p, applied, sources, context, resolve=True))
            plan = candidate
            state["batches"].append({"root_id": str(batch["id"]), "delta": delta})
            persist()

        # Local semantic reconciliation after every entity's schema has been seen.
        for index, record in enumerate(state["fact_batches"]):
            if index < len(state["reconciled"]):
                continue
            facts = record["facts"]
            bindings = state["identity_batches"][index]["bindings"]
            if not bindings:
                state["reconciled"].append(record["root_id"])
                persist()
                continue
            ids = {item["req_id"] for fact in facts for item in fact["evidence"]}
            context = {"entity_index": _entity_index(state["catalog"]),
                       **_schema_context(plan, facts, bindings, ids)}
            await self._log(f"Reconciling related schema {index + 1}/{len(batches)}: {record['root_id']}")
            _, plan = await self._ask(model, "reconcile", context, SchemaDelta.model_json_schema(),
                                      lambda p: _merge_related(plan, p, applied, sources, context, resolve=True))
            state["reconciled"].append(record["root_id"])
            persist()

        state.setdefault("seed_batches", [])
        for index, record in enumerate(state["fact_batches"]):
            if index < len(state["seed_batches"]):
                continue
            facts = [fact for fact in record["facts"] if fact["kind"] == "seed"]
            if facts:
                ids = {item["req_id"] for fact in facts for item in fact["evidence"]}
                context = _schema_context(plan, facts, state["identity_batches"][index]["bindings"], ids)
                context.pop("schema_index")
                context["requirements"] = batches[index]
                context["seed_index"] = [{"table": seed["table"], "source": seed["source"],
                                          "conflict_columns": seed["conflict_columns"], "row_count": len(seed["rows"])}
                                         for seed in plan["seeds"] if seed["table"] in {t["name"] for t in context["draft"]["tables"]}]
                await self._log(f"Generating explicit seed records for {record['root_id']}")
                def accept_seeds(payload):
                    delta = SeedDelta.model_validate(payload).model_dump()
                    allowed = {table["name"] for table in context["draft"]["tables"]}
                    if any(seed["table"] not in allowed or not set(seed["req_ids"]) & ids for seed in delta["seeds"]):
                        raise ValueError("Seed records must target supplied related tables and cite this batch's seed facts")
                    return _merge(plan, {"tables": [], **delta}, applied, sources)
                _, plan = await self._ask(model, "seeds", context, SeedDelta.model_json_schema(),
                                          accept_seeds)
            state["seed_batches"].append(record["root_id"])
            persist()
        plan = _order_seed_dependencies(validate_plan(plan, sources))
        state.update({"complete": True})
        persist()
        return plan
