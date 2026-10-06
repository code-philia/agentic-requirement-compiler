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
from core.database_plan import DatabasePlan, Record, Table, Seed, identifier, ensure_additive, validate_plan, unique_keys
from core.files import read_json_file, write_json_file


class Entity(Record):
    name: str
    purpose: str
    req_ids: list[str]
    identity: str
    identity_keys: list[str]
    aliases: list[str]
    roles: list[str]


class Evidence(Record):
    req_id: str
    quote: str = Field(min_length=1)


class PersistenceFact(Record):
    kind: Literal["entity", "identity_key", "relationship", "state", "seed", "no_persistence"]
    entity: str
    target: str = ""
    detail: str = Field(min_length=1, max_length=3000)
    evidence: list[Evidence] = Field(min_length=1)


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
Structured data lifecycles override prose/GIVEN inference for those identities.
SEED declares startup records. CREATED declares records produced by runtime actions:
design persistence but never bootstrap their scenario examples. DERIVED means action-
derived state, not guessed fixtures; persist only when required, do not invent rows.
Use resolved_data as the authoritative referenced definitions. In seed_rows cite
data_ids for structured SEED obligations. Never cite CREATED/DERIVED as seed sources.
Map business properties to the supplied schema; preserve explicit values, identities,
timestamps and relationships. Descriptive creation/target/icon instructions are not
automatically database columns. Add only necessary stable technical keys/relations.
Return JSON records, not arbitrary INSERT SQL; the compiler emits idempotent SQL.
For legacy identities without structured declarations retain explicit prose seed inference.

Entities phase identifies canonical entities directly from the controlled-size subtree,
using the supplied compact record contract. Include identity keys, relationships
(cardinality/ownership/deletion), persistent states and explicit seed obligations in
the SAME response. Cite supplied source IDs; the compiler supplies original quotes
and requirement IDs. No record is required for nodes without persistence. [] means
this subtree has no persistence obligations. State inferred prerequisites explicitly;
do not pretend an inference was written in the source. Seed obligations describe
pre-existing data, not copied row datasets; the seeds phase sees the original subtree.
Match identity/lifecycle/keys and roles, not spelling. Login user and passenger may share an identity only when evidence
supports this; a passenger may exist without an account. Roles are not automatically
entities, and equal names do not prove equal identities. Existing canonical identities
are immutable. Reuse their exact canonical names or declare distinct identities with a clear reason.
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
If deferred_requirements are supplied, entity analysis was inconclusive, NOT no_persistence.
Analyze their original sources during schema/reconcile phases, reuse indexed identities and
tables, and define only genuinely necessary structures. In seeds phase inspect their Given
data for explicit seed obligations even if there is no seed fact. Never invent fixtures.
"""


def _nodes(tree):
    yield tree
    for child in tree.get("children", []) or []:
        yield from _nodes(child)


ENTITY_OUTPUT = """Return only a JSON array of compact records, without tool calls or prose.
Entity: ["entity", canonical_name, identity_description, [identity_keys], [aliases], [roles], [source_ids]].
Data obligation: [kind, canonical_name, target, detail, [source_ids]].
kind is relationship, identity_key, state, or seed; target is empty except for relationships.
Names are ASCII SQL table names. Every referenced identity must be declared here or already
in entity_index/accepted_entities. Reuse existing names exactly; never redefine their identity.
An entity may have evidence from several nodes. Sources are supplied E-numbers, not copied
quotes or requirement IDs. No per-node records, no bindings, no no_persistence records.
[] is valid when the subtree has no persistence. Inspect ALL descriptions and scenario Given
steps for pre-existing data; describe seed obligations briefly without reproducing row data.
For corrections return only remaining/corrected records; valid records are retained.
Example: [["entity","accounts","Registered account",["email"],["user"],[],["E1"]],
["entity","orders","Purchase order",["order_number"],[],[],["E2"]],
["relationship","orders","accounts","Many orders belong to one account",["E2"]]]."""


def _fact_sources(batch):
    """Assign local references without asking the model to reproduce source text."""
    inventory, sources = [], {}
    for index, node in enumerate(_nodes(batch), 1):
        local = f"N{index}"
        excerpts = []

        def add(field, value):
            if value is None or value == "":
                return
            quote = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            ref = f"E{len(sources) + 1}"
            sources[ref] = {"node": local, "req_id": str(node["id"]), "quote": quote}
            excerpts.append({"ref": ref, "field": field, "text": quote})

        add("name", node.get("name"))
        add("description", node.get("description"))
        add("data", node.get("data"))
        add("resolved_data", node.get("resolved_data"))
        # Include all scenario fields, not only Given; never truncate seed data.
        for i, scenario in enumerate(node.get("scenarios", []) or []):
            for field, value in scenario.items():
                if field == "steps" and isinstance(value, list):
                    for j, step in enumerate(value):
                        add(f"scenarios[{i}].steps[{j}]", step)
                else:
                    add(f"scenarios[{i}].{field}", value)
        if not excerpts:
            add("id", str(node["id"]))
        inventory.append({"node": local, "req_id": str(node["id"]),
                          "type": node.get("type", ""),
                          "children": [str(child["id"]) for child in node.get("children", []) or []],
                          "dependencies": node.get("dependencies") or [], "sources": excerpts})
    return inventory, sources


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
            table["req_ids"] = sorted(set(tables[name]["req_ids"]) | set(table["req_ids"]))
            if name not in replacements:
                # Drafts are not migrations: new non-null columns/unique keys are safe
                # before creation. Preserve existing columns unless explicitly replaced.
                prior = tables[name]
                columns = {column["name"]: column for column in prior["columns"]}
                for column in table["columns"]:
                    if column["name"] in columns and column != columns[column["name"]]:
                        raise ValueError(f"Conflicting draft column {name}.{column['name']}; use resolve with replace_table")
                    columns[column["name"]] = column
                if table["primary_key"] != prior["primary_key"]:
                    raise ValueError(f"Conflicting draft primary key for {name}; use resolve with replace_table")
                table["columns"] = list(columns.values())
                for field in ("unique", "indexes"):
                    table[field] = prior[field] + [item for item in table[field] if item not in prior[field]]
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


def _normalize_seeds(plan, delta):
    """Reuse natural identities, allocate integer IDs and remap batch foreign keys.

    Only mechanical identity differences are resolved here. Conflicting business
    values remain explicit errors rather than a first/last writer wins policy.
    """
    delta = deepcopy(delta)
    delta["seeds"] = _order_seed_dependencies({"tables": plan["tables"], "seeds": delta["seeds"]})["seeds"]
    tables = {table["name"]: table for table in plan["tables"]}
    rows_by_table = {}
    for seed in plan["seeds"]:
        rows_by_table.setdefault(seed["table"], []).extend(seed["rows"])
    remaps = {}
    for seed in delta["seeds"]:
        table = tables.get(seed["table"])
        if not table:
            raise ValueError(f"Unknown seed table: {seed['table']}")
        keys = [key for key in unique_keys(table) if key]
        available = [key for key in keys if all(all(row.get(col) is not None for col in key)
                                               for row in seed["rows"])]
        natural = [key for key in available if key != table["primary_key"]]
        # Prefer a business identity over model-assigned surrogate IDs.
        preferred = (natural or available)
        if preferred:
            seed["conflict_columns"] = (seed["conflict_columns"]
                if seed["conflict_columns"] in natural else preferred[0])
        pk = table["primary_key"]
        integer_pk = (len(pk) == 1 and next(col for col in table["columns"]
                      if col["name"] == pk[0])["type"] == "INTEGER")
        existing = rows_by_table.setdefault(seed["table"], [])
        for row in seed["rows"]:
            for column in table["columns"]:
                ref = column["references"]
                if ref and column["name"] in row:
                    row[column["name"]] = remaps.get(
                        (ref["table"], ref["column"], row[column["name"]]), row[column["name"]])
            matches = [prior for prior in existing if any(
                all(row.get(col) is not None and prior.get(col) == row[col] for col in key)
                for key in natural)]
            if not natural and seed["conflict_columns"] in available:
                matches = [prior for prior in existing if all(
                    prior.get(col) == row[col] for col in seed["conflict_columns"])]
            if matches and any(prior != matches[0] for prior in matches):
                raise ValueError(f"Ambiguous seed identity in {seed['table']}; use one existing identity")
            prior = matches[0] if matches else None
            for col in pk:
                old_id = row.get(col)
                new_id = prior.get(col) if prior else old_id
                if integer_pk and not prior and (old_id is None or any(item.get(col) == old_id for item in existing)):
                    new_id = max([item[col] for item in existing if type(item.get(col)) is int] + [0]) + 1
                if new_id is not None:
                    row[col] = new_id
                if old_id is not None:
                    key = (seed["table"], col, old_id)
                    if key in remaps and remaps[key] != new_id:
                        raise ValueError(f"Ambiguous batch ID in {seed['table']}.{col}; assign distinct temporary IDs")
                    remaps[key] = new_id
            if prior:
                # Omitted fields retain existing values; explicit disagreements are
                # checked after FK remapping by validate_plan.
                for col, value in prior.items():
                    row.setdefault(col, value)
            existing.append(row)
    return delta


def _seed_context(plan, tables, requirements, *, row_limit=80):
    """Bound source context; include matching existing rows before a small sample."""
    text = json.dumps(requirements, ensure_ascii=False).casefold()
    result = []
    for table in tables:
        keys = [key for key in unique_keys(table) if key]
        rows = [row for seed in plan["seeds"] if seed["table"] == table["name"] for row in seed["rows"]]
        distinct = list({json.dumps(row, sort_keys=True, ensure_ascii=False): row for row in rows}.values())
        relevant = [row for row in distinct if any(isinstance(row.get(col), str)
                    and len(row[col]) >= 3 and row[col].casefold() in text
                    for key in keys for col in key)]
        selected = (relevant + [row for row in distinct if row not in relevant])[:row_limit]
        result.append({"table": table["name"], "unique_keys": keys, "row_count": len(distinct),
                       "rows": selected, "omitted_rows": max(0, len(distinct) - len(selected))})
    return result


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

    async def _ask(self, model, phase, context, schema, accept, *, output_contract=None,
                   decode=None, attempts=3, salvage=None):
        from agents.model.prompt_input import format_task_input
        from agents.model.tool_sequence import parse_tool_sequence, tool_contract
        initial = [{"role": "system", "content": DATABASE_PROMPT}, {"role": "user", "content": format_task_input({
            "phase": phase, "app_type": self.app_type, **context,
        }, output_contract or tool_contract(schema))}]
        if output_contract:
            initial[0] = {"role": "system", "content": DATABASE_PROMPT +
                          "\nFor this phase the supplied compact output contract overrides the tool-call format."}
        messages = initial
        rejected_payloads = []
        for attempt in range(attempts):
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
                payload = decode(normalized) if decode else parse_tool_sequence(normalized, schema)
                return payload, accept(payload)
            except ValueError as exc:
                if isinstance(payload, dict):
                    rejected_payloads.append(payload)
                if attempt == attempts - 1:
                    if salvage is not None:
                        fallback = payload
                        if phase == "seeds" and rejected_payloads:
                            # Keep valid rows from earlier attempts even if the last
                            # answer is malformed. Prefer the latest corrected rows.
                            records = []
                            for prior in reversed(rejected_payloads):
                                for record in prior.get("seeds", []):
                                    if record not in records:
                                        records.append(record)
                            fallback = {"seeds": records}
                        return payload, salvage(fallback, str(exc))
                    raise RejectedRecord(exc, payload) from exc
                await self._log(f"{phase}: correcting rejected JSON: {exc}")
                # Keep the task and latest candidate, not every rejected conversation.
                messages = [*initial, {"role": "assistant", "content": content},
                                 {"role": "user", "content": f"Validation rejected your record: {exc}. "
                                  "Return corrected records using the supplied output contract; preserve valid records. "
                                  "The draft has not changed. Never remove source coverage or weaken applied structures to pass validation."}]

    async def run(self, requirement_tree: dict[str, Any], baseline: dict[str, Any], *,
                  revision: str = "", requirement_ids: set[str] | None = None) -> dict[str, Any]:
        from arcbench_agent_runtime.requirement_contracts import resolve_requirement_contracts, validate_seed_data_sources
        requirement_tree = resolve_requirement_contracts(requirement_tree)
        nodes = list(_nodes(requirement_tree))
        def validate_seed_sources(seeds):
            validate_seed_data_sources(seeds, nodes)
        applied = baseline.get("previous_plan") or {"tables": [], "seeds": []}
        sources = set(requirement_ids or ()) | {str(node["id"]) for node in nodes}
        sources.update(rid for item in [*applied["tables"], *applied["seeds"]] for rid in item["req_ids"])
        identity = {"version": 4, "revision": revision, "tree": requirement_tree,
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
        if state.get("seed_protocol") != 2 and not state.get("complete"):
            # Old incomplete runs may contain latent primary-key collisions that
            # previously passed validation. Preserve the draft for inspection and
            # regenerate only bootstrap rows, keeping completed schema analysis.
            if seed_state["seeds"]:
                write_json_file(seeds_path.with_name("seeds.previous.json"), seed_state)
            seed_state["seeds"] = deepcopy(applied["seeds"])
            state["seed_batches"] = []
            state["seed_protocol"] = 2
            write_json_file(seeds_path, seed_state)
            write_json_file(path, state)
            await self._log("Seed protocol upgraded: retaining schema progress; regenerating draft seeds with shared identities.")
        state["plan"]["seeds"] = seed_state["seeds"]
        plan = validate_plan(state["plan"], sources, defer_references=True)
        ensure_additive(applied, plan)
        if state.get("complete") and not baseline.get("previous_failure"):
            return validate_plan(plan, sources, defer_references=True)
        model = create_arc_chat_model(os.environ.get("MODEL", "openai:gpt-5.4"))
        # Row payloads stay in a separate keyed record, never in analysis prompts/history.
        def persist():
            write_json_file(seeds_path, {"key": key, "seeds": plan["seeds"]})
            state["plan"] = {"tables": plan["tables"]}
            write_json_file(path, state)

        def diagnostic(phase, record, error, req_ids):
            state.setdefault("skipped_records", []).append({
                "phase": phase, "table": record.get("table", record.get("name")) if isinstance(record, dict) else None,
                "record": record, "error": error, "req_ids": sorted(req_ids),
            })

        def salvage_schema(payload, error, context):
            candidate = plan
            records = payload.get("tables", []) if isinstance(payload, dict) else []
            if not records:
                req_ids = {str(node["id"]) for node in _nodes(context.get("requirements", {})) if "id" in node}
                req_ids.update(item["req_id"] for fact in context.get("facts", []) for item in fact["evidence"])
                req_ids.update(item["req_id"] for item in context.get("deferred_requirements", []))
                diagnostic("schema", payload, error, req_ids)
            for record in records:
                try:
                    replacements = payload.get("replace_tables", [])
                    candidate = _merge_related(candidate, {
                        "tables": [record], "replace_tables": [record.get("name")] if record.get("name") in replacements else [],
                    }, applied, sources, context, resolve=True)
                except (ValueError, TypeError, AttributeError) as exc:
                    diagnostic("schema", record, str(exc), record.get("req_ids", []) if isinstance(record, dict) else [])
            return candidate

        root_context = _summary(requirement_tree)
        batches = [{k: v for k, v in requirement_tree.items() if k != "children"},
                   *(requirement_tree.get("children", []) or [])]
        # Identify canonical entities and data obligations together, one subtree at a time.
        # No separate candidate inventory, binding call, or per-node coverage requirement.
        for index, batch in enumerate(batches):
            if index < len(state["identity_batches"]):
                continue
            await self._log(f"Identifying database entities {index + 1}/{len(batches)}: {batch['id']}")
            inventory, source_refs = _fact_sources(batch)
            progress = state.setdefault("entity_progress", {}).setdefault(str(batch["id"]),
                         {"entities": [], "facts": []})
            additions, facts = progress["entities"], progress["facts"]

            def accept_entities(rows):
                if not isinstance(rows, list):
                    raise ValueError("Expected a JSON array of entity/relationship/state/seed records")
                errors = []
                for position, row in enumerate(rows):
                    try:
                        if not isinstance(row, list) or not row:
                            raise ValueError("Each record must be a nonempty array")
                        kind = row[0]
                        if kind in {"identity_key", "state", "seed"} and len(row) == 4:
                            row = [*row[:2], "", *row[2:]]
                        if kind in {"identity_key", "state", "seed"} and len(row) == 5 and row[2]:
                            # Models often put a key/state name in the relationship-only
                            # slot. Keep that information as detail instead of retrying.
                            row = [*row[:2], "", f"{row[2]}: {row[3]}", row[4]]
                        expected = 7 if kind == "entity" else 5
                        if len(row) != expected:
                            raise ValueError(f"{kind} requires {expected} fields; follow the supplied compact contract")
                        refs = [row[-1]] if isinstance(row[-1], str) else row[-1]
                        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
                            raise ValueError("Provide source IDs from this batch")
                        if any(ref not in source_refs for ref in refs):
                            raise ValueError("Unknown source ID")
                        evidence = [{"req_id": source_refs[ref]["req_id"], "quote": source_refs[ref]["quote"]}
                                    for ref in dict.fromkeys(refs)]
                        req_ids = sorted({item["req_id"] for item in evidence})
                        if kind == "entity":
                            _, name, description, keys, aliases, roles, _ = row
                            identifier(name)
                            entity = Entity.model_validate({"name": name, "purpose": description,
                                "identity": description, "identity_keys": keys, "aliases": aliases,
                                "roles": roles, "req_ids": req_ids}).model_dump()
                            if not description.strip():
                                raise ValueError("Describe entity identity/lifecycle")
                            existing = next((item for item in state["catalog"] if item["name"] == name), None)
                            prior = next((item for item in additions if item["name"] == name), None)
                            fact = PersistenceFact.model_validate({"kind": "entity", "entity": name,
                                    "target": "", "detail": existing["identity"] if existing else description,
                                    "evidence": evidence}).model_dump()
                            if not existing and not prior:
                                if name.casefold() in {item["name"].casefold() for item in [*state["catalog"], *additions]}:
                                    raise ValueError("Reuse the exact existing canonical name")
                                additions.append(entity)
                            elif prior:
                                if prior["identity"] != description or prior["identity_keys"] != keys:
                                    raise ValueError(f"Conflicting identity for {name}; preserve accepted identity")
                                prior["req_ids"] = sorted(set(prior["req_ids"]) | set(req_ids))
                                prior["aliases"] = sorted(set(prior["aliases"]) | set(aliases))
                                prior["roles"] = sorted(set(prior["roles"]) | set(roles))
                            elif existing:
                                existing["aliases"] = sorted(set(existing["aliases"]) | set(aliases))
                                existing["roles"] = sorted(set(existing["roles"]) | set(roles))
                        elif kind in {"relationship", "identity_key", "state", "seed"}:
                            _, name, target, detail, _ = row
                            identifier(name)
                            if kind == "relationship":
                                identifier(target)
                            elif target:
                                raise ValueError("Only relationships have a target")
                            fact = {"kind": kind, "entity": name, "target": target,
                                    "detail": detail, "evidence": evidence}
                        else:
                            raise ValueError("Only entity, relationship, identity_key, state and seed records are needed")
                        fact = PersistenceFact.model_validate(fact).model_dump()
                        if fact not in facts:
                            facts.append(fact)
                    except (ValueError, TypeError) as exc:
                        errors.append(f"Record {position + 1}: {str(exc)[:350]}")
                persist()  # Retain valid rows even when another row requires correction.
                if errors:
                    raise ValueError("\n".join(errors[:12]))
                known = {item["name"] for item in [*state["catalog"], *additions]}
                missing = {fact["entity"] for fact in facts} - known
                missing.update(fact["target"] for fact in facts
                               if fact["kind"] == "relationship" and fact["target"] not in known)
                if missing:
                    raise ValueError("Declare identities for these referenced entities: " + ", ".join(sorted(missing)))
                return facts

            deferred = []
            try:
                await self._ask(model, "entities", {
                    "root_summary": root_context, "nodes": inventory,
                    "entity_index": _entity_index(state["catalog"]),
                    "accepted_entities": _entity_index(additions),
                    "accepted_obligations": [{k: v for k, v in fact.items() if k != "evidence"} for fact in facts],
                    "instruction": "Identify canonical entities and data obligations directly. Return only new or remaining records. No record is needed for a node without persistence.",
                }, None, accept_entities, output_contract=ENTITY_OUTPUT, decode=json.loads)
            except RejectedRecord as exc:
                # Inconclusive format is not evidence of absent data. Schema and seed
                # analysis inspect the original subtree while retaining accepted records.
                deferred = inventory
                await self._log(f"entities: retained {len(facts)} records; continuing original-source analysis for {batch['id']}: {str(exc)[:500]}")
            state["catalog"].extend(additions)
            bindings = [{"candidate": name, "canonical": name, "reason": "Direct canonical entity identification"}
                        for name in sorted({fact["entity"] for fact in facts}
                            | {fact["target"] for fact in facts if fact["kind"] == "relationship"})]
            for entity in state["catalog"]:
                own_facts = [fact for fact in facts if entity["name"] in {fact["entity"], fact["target"]}]
                entity["req_ids"] = sorted(set(entity["req_ids"]) |
                    {item["req_id"] for fact in own_facts for item in fact["evidence"]})
            state["fact_batches"].append({"root_id": str(batch["id"]), "facts": facts,
                                          "deferred_requirements": deferred})
            state["identity_batches"].append({"root_id": str(batch["id"]), "bindings": bindings})
            state["entity_progress"].pop(str(batch["id"]), None)
            persist()

        for index, batch in enumerate(batches):
            if index < len(state["batches"]):
                continue
            await self._log(f"Analyzing database batch {index + 1}/{len(batches)}: {batch['id']}")
            facts = state["fact_batches"][index]["facts"]
            bindings = state["identity_batches"][index]["bindings"]
            deferred = state["fact_batches"][index].get("deferred_requirements", [])
            if not bindings and not deferred:
                state["batches"].append({"root_id": str(batch["id"]), "delta": {"tables": []}})
                persist()
                continue
            batch_ids = {str(node["id"]) for node in _nodes(batch)}
            context = {"root_summary": root_context, "entity_index": _entity_index(state["catalog"]),
                       "requirements": batch, "deferred_requirements": deferred,
                       **_schema_context(plan, facts, bindings, batch_ids)}
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
                    SchemaDelta.model_json_schema(), lambda p: _merge_related(plan, p, applied, sources, context, resolve=True),
                    salvage=lambda p, error: salvage_schema(p, error, context))
            plan = candidate
            if deferred:
                # Deferred analysis can discover an identity without an earlier binding.
                # Register its real table and keys for all subsequent batches to reuse.
                known = {entity["name"] for entity in state["catalog"]}
                for table in plan["tables"]:
                    if table["name"] not in known and set(table["req_ids"]) & batch_ids:
                        state["catalog"].append({"name": table["name"], "purpose": "Persistent entity",
                            "identity": "Schema-confirmed table: " + table["name"],
                            "identity_keys": [",".join(table["primary_key"])], "aliases": [],
                            "roles": [], "req_ids": table["req_ids"]})
            state["batches"].append({"root_id": str(batch["id"]), "delta": delta})
            persist()

        # Local semantic reconciliation after every entity's schema has been seen.
        for index, record in enumerate(state["fact_batches"]):
            if index < len(state["reconciled"]):
                continue
            facts = record["facts"]
            bindings = state["identity_batches"][index]["bindings"]
            deferred = record.get("deferred_requirements", [])
            if not bindings and not deferred:
                state["reconciled"].append(record["root_id"])
                persist()
                continue
            ids = {item["req_id"] for fact in facts for item in fact["evidence"]}
            ids.update(item["req_id"] for item in deferred)
            context = {"entity_index": _entity_index(state["catalog"]),
                       "deferred_requirements": deferred,
                       **_schema_context(plan, facts, bindings, ids)}
            await self._log(f"Reconciling related schema {index + 1}/{len(batches)}: {record['root_id']}")
            _, plan = await self._ask(model, "reconcile", context, SchemaDelta.model_json_schema(),
                                      lambda p: _merge_related(plan, p, applied, sources, context, resolve=True),
                                      salvage=lambda p, error: salvage_schema(p, error, context))
            state["reconciled"].append(record["root_id"])
            persist()

        state.setdefault("seed_batches", [])
        for index, record in enumerate(state["fact_batches"]):
            if index < len(state["seed_batches"]):
                continue
            facts = [fact for fact in record["facts"] if fact["kind"] == "seed"]
            deferred = record.get("deferred_requirements", [])
            structured_seed_nodes = [node for node in _nodes(batches[index])
                                     if any(entry["lifecycle"] == "SEED"
                                            for entry in node.get("resolved_data", []))]
            if facts or deferred or structured_seed_nodes:
                ids = {item["req_id"] for fact in facts for item in fact["evidence"]}
                ids.update(item["req_id"] for item in deferred)
                ids.update(str(node["id"]) for node in structured_seed_nodes)
                context = _schema_context(plan, facts, state["identity_batches"][index]["bindings"], ids)
                context["deferred_requirements"] = deferred
                context.pop("schema_index")
                context["requirements"] = batches[index]
                context["seed_data"] = [entry for node in _nodes(batches[index])
                                        for entry in node.get("resolved_data", [])
                                        if entry["lifecycle"] == "SEED"]
                context["existing_seed_records"] = _seed_context(plan, context["draft"]["tables"], batches[index])
                context["seed_rule"] = (
                    "Reuse supplied existing records and identities. Do not generate different passwords, timestamps, "
                    "IDs or optional values for the same identity. Emit only explicit startup data obligations; "
                    "scenario-specific action results and incompatible Given states belong in test setup. "
                    "Integer primary IDs may be omitted for new records with an explicit natural unique key; "
                    "the compiler allocates them. Use distinct temporary IDs when other rows reference them. "
                    "The compiler selects a schema-declared conflict key and remaps batch foreign keys. "
                    "If no startup rows are required return [].")
                await self._log(f"Generating explicit seed records for {record['root_id']}")
                def accept_seeds(payload):
                    delta = SeedDelta.model_validate(payload).model_dump()
                    validate_seed_sources(delta["seeds"])
                    allowed = {table["name"] for table in context["draft"]["tables"]}
                    if any(seed["table"] not in allowed or not set(seed["req_ids"]) & ids for seed in delta["seeds"]):
                        raise ValueError("Seed records must target supplied related tables and cite this batch's seed facts")
                    delta = _normalize_seeds(plan, delta)
                    return _merge(plan, {"tables": [], **delta}, applied, sources)

                def salvage_seeds(payload, error):
                    candidate = plan
                    records = payload.get("seeds", []) if isinstance(payload, dict) else []
                    if not records:
                        diagnostic("seeds", payload, error, ids)
                    for seed in records:
                        rows = seed.get("rows", []) if isinstance(seed, dict) else []
                        if not rows:
                            diagnostic("seeds", seed, error, ids)
                        for row in rows:
                            item = {**seed, "rows": [row]}
                            try:
                                delta = SeedDelta.model_validate({"seeds": [item]}).model_dump()
                                validate_seed_sources(delta["seeds"])
                                allowed = {table["name"] for table in context["draft"]["tables"]}
                                if item["table"] not in allowed or not set(item["req_ids"]) & ids:
                                    raise ValueError("Seed record must target a supplied table and cite this batch")
                                delta = _normalize_seeds(candidate, delta)
                                candidate = _merge(candidate, {"tables": [], **delta}, applied, sources)
                            except (ValueError, TypeError, KeyError) as exc:
                                diagnostic("seeds", item, str(exc), ids)
                    return candidate
                _, plan = await self._ask(model, "seeds", context, SeedDelta.model_json_schema(),
                                          accept_seeds, salvage=salvage_seeds)
            state["seed_batches"].append(record["root_id"])
            persist()
        # FK validity and row order are resolved against SQLite in preparation.
        plan = validate_plan(plan, sources, defer_references=True)
        state.update({"complete": True})
        persist()
        return plan
