"""Resumable global database preparation before requirement-node compilation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from agents.database_designer import DatabaseDesigner
from core.config import get_android_package
from core.database_codegen import render_files, runtime_bootstrap_hook, write_generated_files
from core.database_plan import compile_plan, ensure_additive, identifier, literal, validate_plan, unique_keys
from core.files import read_json_file, write_json_file


def application_db_path(workspace: Path, app_type: str) -> Path:
    if app_type == "android":
        return workspace / ".arc/database/android-bootstrap.sqlite"
    root = workspace / "backend" if app_type == "web" else workspace
    value = os.environ.get("ARC_DB_FILE") or os.environ.get("DATABASE_FILE") or "database.db"
    path = (root / value).resolve()
    if not path.is_relative_to(workspace.resolve()):
        raise ValueError("Global database preparation requires a database inside the output workspace")
    return path


def inspect_database(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        return {
            name: {
                "sql": sql,
                "columns": [dict(zip(("cid", "name", "type", "notnull", "default", "pk"), row))
                            for row in db.execute(f'PRAGMA table_info("{name.replace(chr(34), chr(34) * 2)}")')],
                "foreign_keys": db.execute(f'PRAGMA foreign_key_list("{name.replace(chr(34), chr(34) * 2)}")').fetchall(),
                "indexes": [
                    {"name": index[1], "unique": bool(index[2]),
                     "columns": [row[2] for row in db.execute(f'PRAGMA index_info("{index[1].replace(chr(34), chr(34) * 2)}")')]}
                    for index in db.execute(f'PRAGMA index_list("{name.replace(chr(34), chr(34) * 2)}")').fetchall()
                ],
            }
            for name, sql in db.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        }


def apply_sqlite_plan(db: sqlite3.Connection, plan: dict[str, Any]) -> None:
    """Build/update in a transaction, rejecting incompatible existing definitions."""
    program = compile_plan(plan)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("BEGIN IMMEDIATE")
    try:
        for table, compiled in zip(plan["tables"], program["tables"]):
            db.execute(compiled["create"])
            columns = {row[1]: row for row in db.execute(f'PRAGMA table_info("{table["name"]}")')}
            for column in table["columns"]:
                existing = columns.get(column["name"])
                if existing:
                    # SQLite INTEGER PRIMARY KEY implies non-null even if PRAGMA reports 0.
                    if existing[2].upper() != column["type"] or (not column["nullable"] and not existing[3] and not existing[5]):
                        raise ValueError(f"Existing column conflicts with database plan: {table['name']}.{column['name']}")
                    planned_default = literal(column["default"]) if column["default"] is not None else None
                    actual_default = existing[4]
                    if actual_default != planned_default:
                        raise ValueError(f"Existing default conflicts with database plan: {table['name']}.{column['name']}")
                else:
                    if column["name"] in table["primary_key"] or (not column["nullable"] and column["default"] is None):
                        raise ValueError(f"Unsafe column addition: {table['name']}.{column['name']}")
                    db.execute(compiled["add_columns"][column["name"]])
            actual_primary = [row[1] for row in sorted(columns.values(), key=lambda row: row[5]) if row[5]]
            if actual_primary != table["primary_key"]:
                raise ValueError(f"Existing primary key conflicts with database plan: {table['name']}")
            actual_unique = []
            for index in db.execute(f'PRAGMA index_list("{table["name"]}")').fetchall():
                if index[2]:
                    escaped = index[1].replace('"', '""')
                    actual_unique.append([row[2] for row in db.execute(f'PRAGMA index_info("{escaped}")')])
            if any(key not in actual_unique for key in table["unique"]):
                raise ValueError(f"Existing unique constraints conflict with database plan: {table['name']}")
            actual_refs = {(row[3], row[2], row[4], row[6]) for row in db.execute(f'PRAGMA foreign_key_list("{table["name"]}")')}
            for column in table["columns"]:
                ref = column["references"]
                if ref and (column["name"], ref["table"], ref["column"], ref["on_delete"]) not in actual_refs:
                    raise ValueError(f"Existing foreign key conflicts with database plan: {table['name']}.{column['name']}")
        for sql in program["indexes"]:
            db.execute(sql)
        for table in plan["tables"]:
            existing_indexes = {row[1]: row for row in db.execute(f'PRAGMA index_list("{table["name"]}")')}
            for index in table["indexes"]:
                existing = existing_indexes.get(index["name"])
                escaped = index["name"].replace('"', '""')
                columns = [row[2] for row in db.execute(f'PRAGMA index_info("{escaped}")')]
                if not existing or bool(existing[2]) != index["unique"] or columns != index["columns"]:
                    raise ValueError(f"Existing index conflicts with database plan: {index['name']}")
        for sql in program["seeds"]:
            db.execute(sql)
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Database preparation found foreign key violations")
        db.commit()
    except Exception:
        db.rollback()
        raise


def validate_against_database(path: Path, plan: dict[str, Any]) -> None:
    """Validate on a SQLite backup; do not mutate an existing application's data."""
    with sqlite3.connect(":memory:") as scratch:
        if path.exists():
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source:
                source.backup(scratch)
        apply_sqlite_plan(scratch, plan)
        # Execute twice as part of the compiler's idempotence acceptance gate.
        apply_sqlite_plan(scratch, plan)


def isolate_database_records(path: Path, plan: dict[str, Any], previous: dict[str, Any],
                             sources: set[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Select a strict, replayable plan on a backup; never weaken SQLite constraints."""
    from copy import deepcopy
    accepted = deepcopy(previous)
    issues: list[dict[str, Any]] = []
    old_tables = {table["name"]: table for table in previous["tables"]}
    with sqlite3.connect(":memory:") as scratch:
        if path.exists():
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source:
                source.backup(scratch)
        apply_sqlite_plan(scratch, previous)
        for table in plan["tables"]:
            try:
                candidate = {"tables": [table], "seeds": []}
                validate_plan({"tables": [item for item in accepted["tables"] if item["name"] != table["name"]] + [table],
                               "seeds": accepted["seeds"]}, sources, defer_references=True)
                ensure_additive({"tables": [old_tables[table["name"]]] if table["name"] in old_tables else [], "seeds": []}, candidate)
                # Existing apply uses a transaction: one table failure rolls back only
                # this scratch attempt. No failed definition enters generated code.
                apply_sqlite_plan(scratch, candidate)
                accepted["tables"] = [item for item in accepted["tables"] if item["name"] != table["name"]] + [table]
            except (ValueError, sqlite3.Error) as exc:
                issues.append({"phase": "schema", "table": table["name"], "record": table,
                               "req_ids": table["req_ids"], "error": str(exc)})
        # Quarantine referencing definitions transitively rather than removing FKs.
        while True:
            rejected = []
            names = {item["name"]: item for item in accepted["tables"]}
            for table in accepted["tables"]:
                invalid = [col["references"] for col in table["columns"] if col["references"] and (
                    col["references"]["table"] not in names or
                    [col["references"]["column"]] not in unique_keys(names[col["references"]["table"]]))]
                if invalid:
                    rejected.append((table, f"Unavailable foreign key targets: {invalid}"))
            if not rejected:
                break
            for table, error in rejected:
                if table["name"] in old_tables:
                    raise ValueError(f"Previously applied database contract is invalid: {table['name']}: {error}")
                accepted["tables"].remove(table)
                issues.append({"phase": "schema", "table": table["name"], "record": table,
                               "req_ids": table["req_ids"], "error": error})
        accepted = validate_plan(accepted, sources)
        # Rebuild a clean backup: quarantined scratch tables must not help seed FKs.
        with sqlite3.connect(":memory:") as rows_db:
            if path.exists():
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source:
                    source.backup(rows_db)
            apply_sqlite_plan(rows_db, accepted)
            pending = [(seed, row) for seed in plan["seeds"] for row in seed["rows"]
                       if not any(seed["table"] == prior["table"] and row in prior["rows"]
                                  and seed["conflict_columns"] == prior["conflict_columns"] for prior in previous["seeds"])]
            while pending:
                retry = []
                progress = False
                for seed, row in pending:
                    item = {**seed, "rows": [row]}
                    try:
                        candidate = validate_plan({"tables": accepted["tables"], "seeds": [*accepted["seeds"], item]}, sources)
                        rows_db.execute("SAVEPOINT seed_record")
                        try:
                            for sql in compile_plan({"tables": [], "seeds": [item]})["seeds"]:
                                rows_db.execute(sql)
                        except sqlite3.Error:
                            rows_db.execute("ROLLBACK TO seed_record")
                            rows_db.execute("RELEASE seed_record")
                            raise
                        rows_db.execute("RELEASE seed_record")
                        accepted = candidate
                        progress = True
                    except (ValueError, sqlite3.Error) as exc:
                        retry.append((seed, row, str(exc)))
                if not retry:
                    break
                if not progress:
                    issues.extend({"phase": "seeds", "table": seed["table"], "record": {**seed, "rows": [row]},
                                   "req_ids": seed["req_ids"], "error": error} for seed, row, error in retry)
                    break
                pending = [(seed, row) for seed, row, _ in retry]
    ensure_additive(previous, accepted)
    return accepted, issues


def verify_materialized_database(path: Path, plan: dict[str, Any]) -> None:
    if not plan["tables"]:
        return
    if not path.is_file():
        raise ValueError("Database initialization returned without creating the application database")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        for table in plan["tables"]:
            actual = {row[1] for row in db.execute(f'PRAGMA table_info({identifier(table["name"])})')}
            if {column["name"] for column in table["columns"]} - actual:
                raise ValueError(f"Database initialization did not materialize table/columns: {table['name']}")
        for seed in plan["seeds"]:
            predicate = " AND ".join(identifier(column) + " = ?" for column in seed["conflict_columns"])
            for row in seed["rows"]:
                found = db.execute(
                    f"SELECT 1 FROM {identifier(seed['table'])} WHERE {predicate} LIMIT 1",
                    [row[column] for column in seed["conflict_columns"]],
                ).fetchone()
                if not found:
                    raise ValueError(f"Database initialization did not materialize required seed identity: {seed['table']}")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Materialized database has foreign key violations")


def plan_changes(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Return requirements mapped to any changed table or seed group."""
    affected: set[str] = set()
    old_tables = {table["name"]: table for table in previous.get("tables", [])}
    for table in current["tables"]:
        old = old_tables.get(table["name"], {})
        old_seeds = [seed for seed in previous.get("seeds", []) if seed["table"] == table["name"]]
        new_seeds = [seed for seed in current["seeds"] if seed["table"] == table["name"]]
        if old != table or old_seeds != new_seeds:
            affected.update(old.get("req_ids", []))
            affected.update(table["req_ids"])
    return sorted(affected)


class DatabasePreparation:
    def __init__(self, *, workspace_path: str, requirement_path: str, app_type: str, log_cb):
        self.workspace = Path(workspace_path).resolve()
        self.app_type = app_type
        self.log_cb = log_cb
        self.state_path = self.workspace / ".arc/database/state.json"
        self.designer = DatabaseDesigner(
            workspace_root=str(self.workspace), app_type=app_type,
            requirement_path=requirement_path, log_cb=log_cb,
        )

    async def prepare(self, tree: dict[str, Any], revision: str, runtime, *,
                      repair_plan: dict[str, Any] | None = None,
                      repair_baseline: dict[str, Any] | None = None,
                      remaining_issues: list[dict[str, Any]] | None = None,
                      commit: bool = True) -> dict[str, Any]:
        if repair_plan is None:
            from core.database_repair import recover_database_repair
            recover_database_repair(self)
        state = read_json_file(self.state_path) or {}
        previous_error = state.get("error", "")
        previous = repair_baseline if repair_baseline is not None else state.get("applied_plan") or {"tables": [], "seeds": []}
        sources = {item["req_id"] for item in runtime.traceability.list_requirements()}
        # Evolution can preserve tables sourced from the previous requirement document.
        sources.update(req_id for table in previous.get("tables", []) for req_id in table["req_ids"])
        sources.update(req_id for seed in previous.get("seeds", []) for req_id in seed["req_ids"])
        os.environ["ARC_DATABASE_READY"] = "0"
        state.update({"status": "RUNNING", "error": ""})
        write_json_file(self.state_path, state)
        await self._log("Preparing the whole-application database before node compilation.")
        try:
            db_path = application_db_path(self.workspace, self.app_type)
            if repair_plan is not None:
                plan = validate_plan(repair_plan, sources)
            elif (state.get("requirements_revision") == revision
                    and state.get("analysis_validated") and isinstance(state.get("plan"), dict)):
                plan = validate_plan(state.get("requested_plan") or state["plan"], sources, defer_references=True)
                await self._log("Reusing analyzed database records; replaying deterministic materialization and preparation.")
            else:
                payload = await self.designer.run(tree, {
                    "previous_plan": previous, "database_path": str(db_path.relative_to(self.workspace)),
                    "existing_schema": inspect_database(db_path),
                    "previous_failure": previous_error,
                }, revision=revision, requirement_ids=sources)
                plan = validate_plan(payload, sources, defer_references=True)
            requested_plan = plan
            plan, issues = isolate_database_records(db_path, plan, previous, sources)
            analysis = read_json_file(self.workspace / ".arc/database/analysis.json") or {}
            issues = [*(remaining_issues if remaining_issues is not None else analysis.get("skipped_records", [])), *issues]
            # Coverage diagnostics are derived afresh; retaining them would keep a
            # successfully repaired seed's consumers blocked forever.
            issues = [issue for issue in issues if not issue.get("data_id")]
            # Explicit seeds are mandatory product state. A quarantined or omitted
            # record must not leave its consumers runnable against an empty database.
            records = runtime.traceability.list_requirements()
            seed_consumers: dict[str, set[str]] = {}
            for record in records:
                for entry in record.get("resolved_data", []):
                    if entry["lifecycle"] == "SEED":
                        seed_consumers.setdefault(entry["id"], set()).add(record["req_id"])
            seed_status = {}
            for data_id, consumers in seed_consumers.items():
                locations = [{"table": seed["table"], "conflict_columns": seed["conflict_columns"],
                              "identities": [{key: row[key] for key in seed["conflict_columns"]}
                                             for row in seed["rows"]]}
                             for seed in plan["seeds"] if data_id in seed.get("data_ids", [])]
                expected = [seed for seed in requested_plan["seeds"] if data_id in seed.get("data_ids", [])]
                accepted = [seed for seed in plan["seeds"] if data_id in seed.get("data_ids", [])]
                ready = bool(locations) and all(
                    any(item["table"] == seed["table"] and row in item["rows"] for item in accepted)
                    for seed in expected for row in seed["rows"])
                seed_status[data_id] = {"status": "ACCEPTED" if ready else "MISSING",
                                        "locations": locations, "req_ids": sorted(consumers)}
                if not ready:
                    issues.append({"phase": "seeds", "data_id": data_id,
                                   "req_ids": sorted(consumers),
                                   "error": f"Required SEED {data_id} is missing or partially rejected"})
            state["seed_data_status"] = seed_status
            blocked = {req_id for issue in issues if issue["phase"] == "schema" for req_id in issue.get("req_ids", [])}
            blocked.update(req_id for issue in issues if issue.get("data_id") for req_id in issue.get("req_ids", []))
            # Block requirement dependencies too; leave unrelated node tasks runnable.
            while True:
                expanded = blocked | {record["req_id"] for record in records
                                      if set(record.get("dependencies", [])) & blocked}
                if expanded == blocked:
                    break
                blocked = expanded
            state.update({"requested_plan": requested_plan, "skipped_records": issues,
                          "blocked_node_ids": sorted(blocked), "degraded": bool(issues)})
            write_json_file(self.workspace / ".arc/database/skipped_records.json", {"records": issues})
            if issues:
                await self._log(f"Isolated {len(issues)} database record(s); continuing with the accepted plan; {len(blocked)} node(s) blocked.")
            ensure_additive(previous, plan)
            state.update({"requirements_revision": revision,
                          "plan": plan, "analysis_validated": False, "runtime_verified": False})
            write_json_file(self.state_path, state)
            validate_against_database(db_path, plan)
            state["analysis_validated"] = True
            write_json_file(self.state_path, state)
            files = render_files(plan, self.app_type, get_android_package()) if plan["tables"] else {}
            for relative in files:
                if (self.workspace / relative).exists() and relative not in state.get("generated_files", []):
                    # Interrupted preparation may have materialized identical files
                    # before state was persisted. Otherwise preserve unrelated code.
                    if (self.workspace / relative).read_text(encoding="utf-8") != files[relative]:
                        raise ValueError(f"Compiler database output collides with an existing file: {relative}")
            hook_files = runtime_bootstrap_hook(self.workspace, self.app_type, get_android_package()) if plan["tables"] else {}
            files.update(hook_files)
            write_generated_files(self.workspace, files)
            # Include the existing hook path on resume even when no rewrite was necessary.
            hook_paths = [path for path in state.get("generated_files", [])
                          if path not in render_files(plan, self.app_type, get_android_package())]
            state["generated_files"] = sorted(set(files) | set(hook_paths))
            # This is a runtime build step, not model-written or model-executed shell.
            if not plan["tables"]:
                output = "No persistent entities are required; database preparation completed without creating a database."
            elif self.app_type == "web":
                output = await self._prepare_web(db_path)
            else:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(db_path) as db:
                    apply_sqlite_plan(db, plan)
                output = f"Prepared SQLite database: {db_path.relative_to(self.workspace)}"
            validate_against_database(db_path, plan)
            verify_materialized_database(db_path, plan)
            for entry in state["seed_data_status"].values():
                if entry["status"] == "ACCEPTED":
                    entry["status"] = "MATERIALIZED"
            affected = plan_changes(previous, plan)
            # Keep unconsumed schema impact across a crash between preparation and
            # scheduling the incremental node tasks.
            if state.get("impact_revision") == revision:
                affected = sorted(set(affected) | set(state.get("affected_node_ids", [])))
            state.update({
                "status": "COMPLETED", "applied_plan": plan, "error": "",
                "runtime_verified": bool(plan["tables"]) and self.app_type == "web",
                "database_path": str(db_path.relative_to(self.workspace)),
                "affected_node_ids": affected, "impact_revision": revision,
                "code_hashes": {path: hashlib.sha256((self.workspace / path).read_bytes()).hexdigest()
                                for path in state["generated_files"]},
            })
            write_json_file(self.state_path, state)
            os.environ["ARC_DATABASE_READY"] = "1"
            runtime.events.notify_traceability_changed("database_prepared")
            self._register_interfaces(plan, state, runtime)
            if commit:
                runtime.git.commit("ARC DATABASE_PREPARE: materialize shared schema and bootstrap records")
            await self._log(output)
            return state
        except Exception as exc:
            os.environ["ARC_DATABASE_READY"] = "0"
            state.update({"status": "FAILED", "error": str(exc), "runtime_verified": False})
            write_json_file(self.state_path, state)
            await self._log(f"Database preparation failed; node compilation is stopped: {exc}", "error")
            return state

    async def repair(self, node_id: str, problem: str, tree: dict[str, Any], revision: str, runtime) -> dict[str, Any]:
        from core.database_repair import repair_database
        return await repair_database(self, node_id, problem, tree, revision, runtime)

    def _register_interfaces(self, plan: dict[str, Any], state: dict[str, Any], runtime) -> None:
        primary_file = next((path for path in state["generated_files"] if path.endswith(("arc_database.js", "arc_database.py", "ArcDatabase.java"))), "")
        for table in plan["tables"]:
            runtime.traceability.upsert_interface(
                interface_id=f"GLOBAL:DB:{table['name']}", req_ids=table["req_ids"], type="DB",
                content=json.dumps({
                    "interface_id": f"GLOBAL:DB:{table['name']}", "type": "DB",
                    "name": table["name"], "file_path": primary_file,
                    "responsibility": "Compiler-owned shared persistence contract",
                    "specification": table,
                    "seed_records": [seed for seed in plan["seeds"] if seed["table"] == table["name"]],
                    "callers": [], "callees": [],
                }, ensure_ascii=False),
                file_path=primary_file, first_line=(self.workspace / primary_file).read_text(encoding="utf-8").splitlines()[0],
                implemented=True,
            )

    async def _prepare_web(self, db_path: Path) -> str:
        process = await asyncio.create_subprocess_exec(
            "node", "src/database/verify_runtime.js", cwd=str(self.workspace / "backend"),
            env={**os.environ, "ARC_DB_FILE": str(db_path)},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise ValueError("Database initialization timed out after 60 seconds")
        if process.returncode != 0:
            raise ValueError("Database initialization failed: " + stderr.decode("utf-8", errors="replace")[-4000:])
        return stdout.decode("utf-8", errors="replace") or f"Prepared SQLite database: {db_path.relative_to(self.workspace)}"

    async def _log(self, message: str, status: str | None = None) -> None:
        result = self.log_cb("DatabasePreparation", message, status, None)
        if hasattr(result, "__await__"):
            await result
