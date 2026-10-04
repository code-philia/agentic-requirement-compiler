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
from core.database_plan import compile_plan, ensure_additive, identifier, literal, validate_plan
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

    async def prepare(self, tree: dict[str, Any], revision: str, runtime) -> dict[str, Any]:
        state = read_json_file(self.state_path) or {}
        previous_error = state.get("error", "")
        previous = state.get("applied_plan") or {"tables": [], "seeds": []}
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
            if state.get("requirements_revision") == revision and state.get("analysis_validated") and isinstance(state.get("plan"), dict):
                plan = validate_plan(state["plan"], sources)
                await self._log("Reusing analyzed database records; replaying deterministic materialization and preparation.")
            else:
                payload = await self.designer.run(tree, {
                    "previous_plan": previous, "database_path": str(db_path.relative_to(self.workspace)),
                    "existing_schema": inspect_database(db_path),
                    "previous_failure": previous_error,
                })
                plan = validate_plan(payload, sources)
            ensure_additive(previous, plan)
            state.update({"requirements_revision": revision, "plan": plan, "analysis_validated": False})
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
            affected = plan_changes(previous, plan)
            # Keep unconsumed schema impact across a crash between preparation and
            # scheduling the incremental node tasks.
            if state.get("impact_revision") == revision:
                affected = sorted(set(affected) | set(state.get("affected_node_ids", [])))
            state.update({
                "status": "COMPLETED", "applied_plan": plan, "error": "",
                "database_path": str(db_path.relative_to(self.workspace)),
                "affected_node_ids": affected, "impact_revision": revision,
                "code_hashes": {path: hashlib.sha256((self.workspace / path).read_bytes()).hexdigest()
                                for path in state["generated_files"]},
            })
            write_json_file(self.state_path, state)
            os.environ["ARC_DATABASE_READY"] = "1"
            runtime.events.notify_traceability_changed("database_prepared")
            self._register_interfaces(plan, state, runtime)
            runtime.git.commit("ARC DATABASE_PREPARE: materialize shared schema and bootstrap records")
            await self._log(output)
            return state
        except Exception as exc:
            os.environ["ARC_DATABASE_READY"] = "0"
            state.update({"status": "FAILED", "error": str(exc)})
            write_json_file(self.state_path, state)
            await self._log(f"Database preparation failed; node compilation is stopped: {exc}", "error")
            return state

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
        script = "const d=require('./src/database/init_db');d.initializeDatabase().then(()=>d.closeDb()).catch(async e=>{console.error(e);try{await d.closeDb()}finally{process.exitCode=1}});"
        process = await asyncio.create_subprocess_exec(
            "node", "-e", script, cwd=str(self.workspace / "backend"),
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
