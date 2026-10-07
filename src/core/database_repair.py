"""Bounded, guarded database repairs initiated by one requirement."""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from agents.database_designer import _merge
from agents.model.factory import create_arc_chat_model
from core.database_plan import Record, Scalar, Table, Seed, ensure_additive, identifier, unique_keys, validate_plan
from core.database_codegen import render_files, runtime_bootstrap_hook
from core.config import get_android_package
from core.files import read_json_file, write_json_file
from pydantic import Field


class SeedCorrection(Record):
    table: str
    key: dict[str, Scalar]
    old_values: dict[str, Scalar]
    new_values: dict[str, Scalar]


class RepairDelta(Record):
    tables: list[Table] = Field(default_factory=list)
    seeds: list[Seed] = Field(default_factory=list)
    corrections: list[SeedCorrection] = Field(default_factory=list)


def _correct(db, corrections):
    for item in corrections:
        assignments = ", ".join(identifier(name) + " = ?" for name in item["new_values"])
        guard = {**item["old_values"], **item["key"]}
        where = " AND ".join(identifier(name) + " IS ?" for name in guard)
        cursor = db.execute(f"UPDATE {identifier(item['table'])} SET {assignments} WHERE {where}",
                            [*item["new_values"].values(), *guard.values()])
        if cursor.rowcount != 1:
            raise ValueError(f"Seed correction guard failed in {item['table']}; current business data cannot be overwritten")


def recover_database_repair(preparation):
    """Restore an interrupted repair before ordinary preparation is resumed."""
    pointer = preparation.workspace / ".arc/database/repair.json"
    record = read_json_file(pointer) or {}
    if record.get("status") != "APPLYING":
        return
    db_path = preparation.workspace / record["database_path"]
    if record.get("backup"):
        with sqlite3.connect(preparation.workspace / record["backup"]) as source, sqlite3.connect(db_path) as target:
            source.backup(target)
    elif db_path.exists():
        db_path.unlink()
    for relative, content in record["original_files"].items():
        path = preparation.workspace / relative
        if content is None:
            if path.exists():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    write_json_file(preparation.state_path, record["original_state"])
    write_json_file(preparation.workspace / ".arc/database/skipped_records.json",
                    {"records": record["original_state"].get("skipped_records", [])})
    record["status"] = "ROLLED_BACK"
    write_json_file(pointer, record)
    write_json_file(preparation.workspace / record["history_path"], record)
    os.environ["ARC_DATABASE_READY"] = "1" if record["original_state"].get("status") == "COMPLETED" else "0"


async def repair_database(preparation, node_id, problem, tree, revision, runtime):
    from core.database import application_db_path, apply_sqlite_plan, inspect_database
    from arcbench_agent_runtime.requirement_contracts import validate_seed_data_sources
    recover_database_repair(preparation)
    original = read_json_file(preparation.state_path) or {}
    previous = original.get("applied_plan") or {"tables": [], "seeds": []}
    sources = {item["req_id"] for item in runtime.traceability.list_requirements()}
    sources.update(rid for item in previous["tables"] + previous["seeds"] for rid in item["req_ids"])
    db_path = application_db_path(preparation.workspace, preparation.app_type)
    issues = [issue for issue in original.get("skipped_records", []) if node_id in issue.get("req_ids", [])]
    data_catalog = {entry["id"]: entry for record in runtime.traceability.list_requirements()
                    for entry in record.get("resolved_data", [])}
    relevant_data_ids = {entry["id"] for entry in
                         (runtime.traceability.get_requirement(node_id) or {}).get("resolved_data", [])}
    relevant_data_ids.update(issue["data_id"] for issue in issues if issue.get("data_id"))
    # A SEED may be declared by ROOT while a leaf consumes it. Preserve the
    # declaring requirement as a valid source when repairing that leaf's gap.
    data_sources = [{"req_id": record["req_id"],
                     "data_ids": [entry["id"] for entry in record.get("resolved_data", [])
                                  if entry["id"] in relevant_data_ids]}
                    for record in runtime.traceability.list_requirements()]
    data_sources = [record for record in data_sources if record["data_ids"]]
    seed_obligations = [seed for seed in (original.get("requested_plan") or {}).get("seeds", [])
                        if relevant_data_ids & set(seed.get("data_ids", []))]

    def accept(payload):
        delta = RepairDelta.model_validate(payload).model_dump()
        validate_seed_data_sources(delta["seeds"], runtime.traceability.list_requirements())
        if not any(delta.values()):
            raise ValueError("Database repair must supply a concrete correction")
        baseline = deepcopy(previous)
        tables = {table["name"]: table for table in previous["tables"]}
        for correction in delta["corrections"]:
            table = tables.get(correction["table"])
            if not table or list(correction["key"]) not in unique_keys(table) or not correction["new_values"]:
                raise ValueError("Corrections require a registered table, unique key and new values")
            if any(value is None for value in correction["key"].values()):
                raise ValueError("Correction keys must be non-null")
            matches = [row for seed in baseline["seeds"] if seed["table"] == table["name"] for row in seed["rows"]
                       if all(row.get(key) == value for key, value in correction["key"].items())]
            if len(matches) != 1 or matches[0] != correction["old_values"]:
                raise ValueError("Only registered bootstrap rows with exact old_values may be corrected")
            identity_columns = {name for key in unique_keys(table) for name in key}
            if any(name in identity_columns and value != matches[0].get(name)
                   for name, value in correction["new_values"].items()):
                raise ValueError("Seed correction cannot change primary/natural identities")
            matches[0].update(correction["new_values"])
        candidate = _merge(baseline, {"tables": delta["tables"], "seeds": delta["seeds"]}, baseline, sources)
        candidate = validate_plan(candidate, sources)
        ensure_additive({"tables": previous["tables"], "seeds": []}, candidate)
        with sqlite3.connect(":memory:") as scratch:
            if db_path.exists():
                with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as source:
                    source.backup(scratch)
            apply_sqlite_plan(scratch, {"tables": candidate["tables"], "seeds": []})
            with scratch:
                _correct(scratch, delta["corrections"])
            apply_sqlite_plan(scratch, candidate)
        return candidate, baseline, delta

    model = create_arc_chat_model(os.environ.get("MODEL", "openai:gpt-5.4"))
    _, (candidate, baseline, delta) = await preparation.designer._ask(model, "repair", {
        "requirement": runtime.traceability.get_requirement(node_id), "problem": problem,
        "missing_records": issues,
        "data_contracts": [data_catalog[data_id] for data_id in sorted(relevant_data_ids)],
        "data_sources": data_sources,
        "requested_seed_records": seed_obligations,
        # Small application databases: always supply the complete schema and
        # bootstrap identities, including dependencies unrelated to this node.
        "tables": previous["tables"],
        "seeds": previous["seeds"],
        "existing_schema": inspect_database(db_path),
        "repair_rule": "Repair only this concrete gap using define_table/seed_rows/correct_seed. Preserve all existing columns, types, defaults, keys and foreign keys. Add tables/nullable columns/indexes or safe defaulted columns. For incorrect bootstrap values use correct_seed with an existing unique key, exact complete old_values and changed new_values. Never change row identities or overwrite live business data. Never invent scenario fixtures. No SQL, dropping, renaming, type changes, weakening constraints or destructive migration. Include complete definitions of modified tables; do not return unchanged rows.",
    }, RepairDelta.model_json_schema(), accept)

    token = uuid4().hex
    backup = preparation.workspace / f".arc/database/backups/{token}.sqlite"
    if db_path.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(backup) as target:
            source.backup(target)
    prospective = render_files(candidate, preparation.app_type, get_android_package())
    prospective.update(runtime_bootstrap_hook(preparation.workspace, preparation.app_type, get_android_package()))
    paths = set(prospective) | set(original.get("generated_files", []))
    record = {"status": "APPLYING", "node_id": node_id, "problem": problem, "delta": delta,
              "database_path": str(db_path.relative_to(preparation.workspace)),
              "backup": str(backup.relative_to(preparation.workspace)) if backup.exists() else None,
              "history_path": f".arc/database/repairs/{token}.json", "original_state": original,
              "original_files": {path: (preparation.workspace / path).read_text(encoding="utf-8")
                                 if (preparation.workspace / path).exists() else None for path in paths}}
    pointer = preparation.workspace / ".arc/database/repair.json"
    write_json_file(pointer, record)
    write_json_file(preparation.workspace / record["history_path"], record)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as db:
            apply_sqlite_plan(db, {"tables": candidate["tables"], "seeds": []})
            with db:
                _correct(db, delta["corrections"])
        fixed_tables = {table["name"] for table in delta["tables"]}
        fixed_seeds = {seed["table"] for seed in delta["seeds"]} | {item["table"] for item in delta["corrections"]}
        remaining = [issue for issue in original.get("skipped_records", [])
                     if not issue.get("data_id") and not (issue.get("table") in (fixed_tables if issue["phase"] == "schema" else fixed_seeds)
                             and node_id in issue.get("req_ids", []))]
        state = await preparation.prepare(tree, revision, runtime, repair_plan=candidate,
                                          repair_baseline=baseline, remaining_issues=remaining, commit=False)
        if state.get("status") != "COMPLETED" or node_id in state.get("blocked_node_ids", []):
            raise ValueError(state.get("error") or "Database repair did not resolve this node's schema gap")
        record["status"] = "COMPLETED"
        write_json_file(pointer, record)
        write_json_file(preparation.workspace / record["history_path"], record)
        return state
    except Exception as exc:
        recover_database_repair(preparation)
        failed = read_json_file(pointer)
        failed["error"] = str(exc)
        write_json_file(pointer, failed)
        write_json_file(preparation.workspace / record["history_path"], failed)
        raise
