"""Resumable subtree database analysis through plain LLM JSON conversations."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import Field

from agents.model.factory import create_arc_chat_model
from agents.model.native_openai import generate_text
from core.database_plan import DatabasePlan, Record, Table, Seed, compile_plan, ensure_additive, validate_plan, unique_keys
from core.files import read_json_file, write_json_file


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


DATABASE_ANALYSIS_VERSION = 6

DATABASE_PROMPT = """Design ARC's shared SQLite database before node design and TDD.
Return only a JSON array of supplied tool calls, never SQL, code, documents or prose.
Schema phase: read the supplied child subtree directly and define the persistent
entities, attributes, keys, relationships and states required by its behavior.
ROOT is not analyzed for schema. Startup properties referenced from ROOT are
withheld; their identity/lifecycle metadata can clarify entity identity.
Reuse the accumulated schema; do not create parallel tables for the same identity.
Return define_table only for new or changed tables, using complete definitions.
Preserve earlier columns, constraints and requirement coverage; add relevant req_ids.
Use ASCII SQL names, INTEGER/REAL/TEXT/BLOB/NUMERIC columns, literal defaults,
explicit non-null primary keys and valid unique/foreign-key constraints.
Roles do not automatically imply separate entities. Include persistent identity/session
prerequisites when behavior requires them; do not design application APIs.
Forward foreign keys may target later subtrees and must resolve after all batches.
[] means this subtree requires no changes. Never emit seeds during schema analysis.
For conflicting draft definitions use replace_table with the corrected complete
definition, preserving existing columns and requirement coverage. Applied schema
and seeds are immutable: only safe additive evolution is permitted.
There is no entity/fact/binding output and no routine reconciliation pass.
Schema_repair runs only after deterministic validation fails: correct the supplied
error without dropping obligations or weakening constraints to pass validation.

Seeds phase: use only ROOT data and the supplied completed schema. Map explicit
SEED properties into concrete rows, including association tables and foreign keys.
properties may be a mapping or a list of mappings. Preserve either shape: a list
declares multiple records; map every record using the completed schema. A mapping
may contain nested collections or descriptive constraints, not just one flat row.
SEED means startup data; CREATED and DERIVED must never produce bootstrap rows.
Cite declared SEED data_ids and ROOT's requirement ID in every structured seed group.
Do not create or change tables/columns in seeds phase. Unknown fields/tables are errors.
Descriptive creation/target/icon instructions are not automatically database columns.
Preserve explicit identities, values, timestamps and relationships; do not invent fixtures.
For legacy ROOT data without lifecycle, seed only explicitly pre-existing records.
Use stable unique conflict_columns and emit parents before children. Reuse supplied
existing identities/values. Integer surrogate IDs may be omitted when a natural unique
key is supplied; use distinct temporary IDs when new rows refer to one another.
No plaintext passwords in hash fields, dynamic placeholders or JSON BLOB values.
Return seed_rows JSON, not INSERT SQL. The compiler generates idempotent code.
[] means no additional startup rows are necessary.

Output format and examples (illustrative only; use actual supplied requirements,
tables and data IDs, never create these example records unless required):
1. Return a bare JSON array. All arguments are siblings of tool. The available
tools' parameters object is contract metadata, NOT the shape of a returned call.
WRONG: [{"tool":"define_table","parameters":{"name":"label","req_ids":["REQ-1.1"],"columns":[]}}]
RIGHT minimal table:
[{"tool":"define_table","name":"workspace","req_ids":["REQ-1.1"],"columns":[{"name":"workspace_id","type":"TEXT","nullable":false}],"primary_key":["workspace_id"]}]
Do not return {"tables":[...]}, {"tools":[...]}, arguments/function wrappers,
Markdown fences, explanations, comments or trailing commas. Optional unique and
indexes may be omitted when unnecessary. Use [] only when no changes are needed.

2. columns is an array of objects, not a name-to-type mapping. Use only the
documented fields; do not invent column-level primary_key, unique, auto_increment,
enum, check or SQL expressions. Keys belong to the table. primary_key is an array
of names; unique is an array of name arrays. Every named column must exist.
WRONG: "primary_key":"label_id", "unique":["workspace_id","name"]
RIGHT: "primary_key":["label_id"], "unique":[["workspace_id","name"]]
Full table with a composite unique key and foreign key:
[{"tool":"define_table","name":"label","req_ids":["REQ-1.1"],"columns":[{"name":"label_id","type":"TEXT","nullable":false},{"name":"workspace_id","type":"TEXT","nullable":false,"references":{"table":"workspace","column":"workspace_id","on_delete":"CASCADE"}},{"name":"name","type":"TEXT","nullable":false},{"name":"is_default","type":"INTEGER","nullable":false,"default":0}],"primary_key":["label_id"],"unique":[["workspace_id","name"]],"indexes":[{"name":"idx_label_workspace","columns":["workspace_id"]}]}]

3. references is an object (table, column, optional on_delete), not "workspace.id"
or SQL. Its target column must be individually unique when schema is complete.
SET NULL requires a nullable referencing column. Every primary-key column must
be nullable:false. Defaults are JSON literals (0, false, "active", null), never
CURRENT_TIMESTAMP, datetime('now') or a function call. Supply runtime timestamps
from application code; explicit seed timestamps are ordinary string values.
An index has name, columns and optional unique. Preserve existing indexes:
do not reuse an index name for different columns, even on another table.

4. seed_rows uses exact schema columns and a real declared unique/primary key.
For the label example above, name alone is NOT unique and id does NOT exist.
WRONG: "conflict_columns":["name"] or ["id"]
RIGHT: "conflict_columns":["workspace_id","name"]
Example parent then child (seeds/repair phases only):
[{"tool":"seed_rows","table":"workspace","req_ids":["ROOT"],"source":"Declared default workspace","data_ids":["DATA-WORKSPACE"],"conflict_columns":["workspace_id"],"rows":[{"workspace_id":"personal"}]},{"tool":"seed_rows","table":"label","req_ids":["ROOT"],"source":"Declared Reminders label","data_ids":["DATA-REMINDERS"],"conflict_columns":["workspace_id","name"],"rows":[{"label_id":"reminders","workspace_id":"personal","name":"Reminders","is_default":1}]}]
rows is an array of flat column/value objects with scalar JSON values; no nested
record objects or arrays. Include every non-null column without a default,
including TEXT primary keys and foreign keys. Reuse existing parent identities.
Never invent a parent fixture from this example: use declared startup requirements
and legitimate application initialization conventions. CREATED/DERIVED are not seeds.

5. replace_table marks a conflicting DRAFT definition; it does not carry columns.
Return it beside a define_table call containing the complete corrected definition:
[{"tool":"replace_table","name":"workspace"},{"tool":"define_table","name":"workspace","req_ids":["REQ-1.1"],"columns":[{"name":"workspace_id","type":"TEXT","nullable":false}],"primary_key":["workspace_id"]}]
Use replacement only when the current phase offers replace_table. Preserve all
earlier required columns/constraints; never use this example to shrink a real table.
For repair, use only tools in that call's available contract; applied schemas remain
subject to additive-change restrictions. correct_seed updates bootstrap VALUES,
not schema or row identities: key must be unique and old_values the exact full row.
Example for an existing label row with an incorrect bootstrap flag:
[{"tool":"correct_seed","table":"label","key":{"label_id":"reminders"},"old_values":{"label_id":"reminders","workspace_id":"personal","name":"Reminders","is_default":0},"new_values":{"is_default":1}}]
"""


def _nodes(tree):
    yield tree
    for child in tree.get("children", []) or []:
        yield from _nodes(child)


def _schema_batch(batch, root_seed_ids):
    """Withhold ROOT startup values while preserving referenced identity metadata."""
    result = deepcopy(batch)
    for node in _nodes(result):
        for field in ("data", "resolved_data"):
            entries = node.get(field)
            if isinstance(entries, list):
                node[field] = [{key: value for key, value in entry.items() if key != "properties"}
                               if isinstance(entry, dict) and entry.get("id") in root_seed_ids
                               else entry for entry in entries]
    return result


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
        raise ValueError("Draft replacements require explicit correction")
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
                        raise ValueError(f"Conflicting draft column {name}.{column['name']}; use replace_table")
                    columns[column["name"]] = column
                if table["primary_key"] != prior["primary_key"]:
                    raise ValueError(f"Conflicting draft primary key for {name}; use replace_table")
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

    async def _ask(self, model, phase, context, schema, accept, *, attempts=3, salvage=None):
        from agents.model.prompt_input import format_task_input
        from agents.model.tool_sequence import parse_tool_sequence, tool_contract
        initial = [{"role": "system", "content": DATABASE_PROMPT}, {"role": "user", "content": format_task_input({
            "phase": phase, "app_type": self.app_type, **context,
        }, tool_contract(schema))}]
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
                payload = parse_tool_sequence(normalized, schema)
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
        tree = resolve_requirement_contracts(requirement_tree)
        nodes = list(_nodes(tree))
        root = {key: value for key, value in tree.items() if key != "children"}
        root_data = root.get("data")
        seed_data = [entry for entry in root_data if isinstance(entry, dict)
                     and entry.get("lifecycle") == "SEED"] if isinstance(root_data, list) else []
        seed_ids = {entry["id"] for entry in seed_data}
        batches = [_schema_batch(batch, seed_ids) for batch in tree.get("children", []) or []]
        batch_order = [str(batch["id"]) for batch in batches]
        applied = baseline.get("previous_plan") or {"tables": [], "seeds": []}
        sources = set(requirement_ids or ()) | {str(node["id"]) for node in nodes}
        sources.update(rid for item in [*applied["tables"], *applied["seeds"]] for rid in item["req_ids"])
        identity = {"version": DATABASE_ANALYSIS_VERSION, "revision": revision,
                    "tree": tree, "batch_order": batch_order, "applied": applied,
                    "schema": baseline.get("existing_schema")}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = Path(self.workspace_root) / ".arc/database/analysis.json"
        schema_path, seeds_path = path.with_name("schema.json"), path.with_name("seeds.json")
        state = read_json_file(path) or {}
        if state.get("key") != key:
            state = {"key": key, "version": DATABASE_ANALYSIS_VERSION, "revision": revision,
                     "batch_order": batch_order, "batches": [], "schema_checked": False,
                     "seed_complete": False, "complete": False, "skipped_records": []}
            write_json_file(schema_path, {"key": key, "tables": deepcopy(applied["tables"])})
            write_json_file(seeds_path, {"key": key, "seeds": deepcopy(applied["seeds"])})
            write_json_file(path, state)
        schema_state, seed_state = read_json_file(schema_path) or {}, read_json_file(seeds_path) or {}
        if schema_state.get("key") != key or seed_state.get("key") != key:
            raise ValueError("Database intermediate records are missing or belong to another analysis revision")
        plan = validate_plan({"tables": schema_state["tables"], "seeds": seed_state["seeds"]},
                             sources, defer_references=True)
        ensure_additive(applied, plan)
        if state.get("complete"):
            return plan
        model = create_arc_chat_model(os.environ.get("MODEL", "openai:gpt-5.4"))

        def persist():
            write_json_file(schema_path, {"key": key, "tables": plan["tables"]})
            write_json_file(seeds_path, {"key": key, "seeds": plan["seeds"]})
            write_json_file(path, state)

        def diagnostic(phase, record, error, req_ids):
            state["skipped_records"].append({
                "phase": phase,
                "table": record.get("table", record.get("name")) if isinstance(record, dict) else None,
                "record": record, "error": str(error), "req_ids": sorted(req_ids),
            })

        def merge_schema(current, payload):
            delta = SchemaDelta.model_validate(payload).model_dump()
            previous = {table["name"]: table for table in current["tables"]}
            for table in delta["tables"]:
                prior = previous.get(table["name"])
                if prior and table["name"] in delta["replace_tables"]:
                    if not {col["name"] for col in prior["columns"]} <= {col["name"] for col in table["columns"]}:
                        raise ValueError("Schema corrections cannot remove earlier columns")
                    for field in ("unique", "indexes"):
                        table[field] = prior[field] + [item for item in table[field] if item not in prior[field]]
            return _merge(current, {**delta, "seeds": []}, applied, sources, resolve=True)

        def salvage_schema(payload, error, req_ids):
            candidate = plan
            records = payload.get("tables", []) if isinstance(payload, dict) else []
            if not records:
                diagnostic("schema", payload, error, req_ids)
            for record in records:
                try:
                    replacements = payload.get("replace_tables", [])
                    candidate = merge_schema(candidate, {
                        "tables": [record],
                        "replace_tables": [record.get("name")] if record.get("name") in replacements else [],
                    })
                except (ValueError, TypeError, AttributeError) as exc:
                    diagnostic("schema", record, exc, req_ids)
            return candidate

        # Each subtree directly extends the schema seen by subsequent subtrees.
        for index, batch in enumerate(batches):
            if index < len(state["batches"]):
                continue
            ids = {str(node["id"]) for node in _nodes(batch)}
            await self._log(f"Designing database schema {index + 1}/{len(batches)}: {batch['id']}")
            _, plan = await self._ask(model, "schema", {
                "requirements": batch, "draft": {"tables": plan["tables"]},
                "existing_schema": baseline.get("existing_schema") or {},
            }, SchemaDelta.model_json_schema(), lambda payload: merge_schema(plan, payload),
                salvage=lambda payload, error: salvage_schema(payload, error, ids))
            state["batches"].append(str(batch["id"]))
            persist()

        def check_schema(candidate):
            schema = validate_plan({"tables": candidate["tables"], "seeds": []}, sources)
            # Check actual SQLite DDL as well as JSON/foreign-key structure.
            with sqlite3.connect(":memory:") as scratch:
                program = compile_plan(schema)
                for table in program["tables"]:
                    scratch.execute(table["create"])
                for sql in program["indexes"]:
                    scratch.execute(sql)

        if not state["schema_checked"]:
            try:
                check_schema(plan)
            except (ValueError, sqlite3.Error) as exc:
                await self._log(f"Repairing database schema after validation: {exc}")
                def accept_repair(payload):
                    candidate = merge_schema(plan, payload)
                    try:
                        check_schema(candidate)
                    except sqlite3.Error as error:
                        raise ValueError(str(error)) from error
                    return candidate
                ids = {str(node["id"]) for batch in batches for node in _nodes(batch)}
                _, plan = await self._ask(model, "schema_repair", {
                    "error": str(exc), "requirements": batches,
                    "draft": {"tables": plan["tables"]},
                }, SchemaDelta.model_json_schema(), accept_repair,
                    salvage=lambda payload, error: salvage_schema(payload, error, ids))
                # The preparation layer quarantines any remaining invalid structures.
            state["schema_checked"] = True
            persist()

        # Only ROOT supplies startup data. Empty structured CREATED/DERIVED-only
        # declarations require no model call; legacy ROOT data remains supported.
        legacy_data = root_data if not isinstance(root_data, list) else [
            entry for entry in root_data if not isinstance(entry, dict) or "lifecycle" not in entry]
        if not state["seed_complete"]:
            if seed_data or legacy_data:
                seed_source = {"seed_data": seed_data, "legacy_data": legacy_data}
                context = {
                    "requirement_id": str(root["id"]), **seed_source,
                    "draft": {"tables": plan["tables"]},
                    "existing_seed_records": _seed_context(plan, plan["tables"], seed_source),
                }
                def merge_seeds(current, payload):
                    delta = SeedDelta.model_validate(payload).model_dump()
                    validate_seed_data_sources(delta["seeds"], nodes)
                    for seed in delta["seeds"]:
                        if str(root["id"]) not in seed["req_ids"]:
                            raise ValueError("Seed records must cite ROOT")
                        if set(seed.get("data_ids", [])) - seed_ids:
                            raise ValueError("Seed sources must be declared in ROOT.data")
                        if seed_ids and not seed.get("data_ids"):
                            raise ValueError("Structured ROOT seeds must cite SEED data_ids")
                    delta = _normalize_seeds(current, delta)
                    return _merge(current, {"tables": [], **delta}, applied, sources)

                def salvage_seeds(payload, error):
                    candidate = plan
                    records = payload.get("seeds", []) if isinstance(payload, dict) else []
                    if not records:
                        diagnostic("seeds", payload, error, {str(root["id"])})
                    for seed in records:
                        rows = seed.get("rows", []) if isinstance(seed, dict) else []
                        if not rows:
                            diagnostic("seeds", seed, error, {str(root["id"])})
                        for row in rows:
                            item = {**seed, "rows": [row]}
                            try:
                                candidate = merge_seeds(candidate, {"seeds": [item]})
                            except (ValueError, TypeError, KeyError) as exc:
                                diagnostic("seeds", item, exc, {str(root["id"])})
                    return candidate

                await self._log("Mapping ROOT SEED data onto the completed database schema")
                _, plan = await self._ask(model, "seeds", context, SeedDelta.model_json_schema(),
                    lambda payload: merge_seeds(plan, payload), salvage=salvage_seeds)
            state["seed_complete"] = True
            persist()
        plan = validate_plan(plan, sources, defer_references=True)
        state["complete"] = True
        persist()
        return plan
