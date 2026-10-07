"""Small-database SQL generation: subtree DDL, one seed batch, bounded repairs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from agents.database_designer import _nodes, _schema_batch
from agents.model.factory import create_arc_chat_model
from agents.model.native_openai import generate_text
from agents.model.prompt_input import format_task_input
from agents.model.tool_sequence import parse_tool_sequence, tool_contract
from agents.runtime.plain_codegen import Record, AddFile, EditFile, DeleteAction, Replacement, NewFile, DeleteFile
from core.config import get_android_package
from core.database_codegen import fixed_web_runtime, render_files, runtime_bootstrap_hook, write_generated_files
from core.files import read_json_file, write_json_file
from pydantic import Field


SQL_ANALYSIS_VERSION = 8


class SQLFiles(Record):
    changes: list[Replacement] = Field(default_factory=list)
    actions: list[AddFile | EditFile | DeleteAction] = Field(default_factory=list)
    new_files: list[NewFile] = Field(default_factory=list)
    delete_files: list[DeleteFile] = Field(default_factory=list)


SQL_PROMPT = """Prepare a SQLite application's database as executable SQL source files.
Return only a JSON array of edit_file/add_file/delete_file calls, with arguments
directly beside tool. The task supplies current SQL source snapshots and the
accumulated schema. One generation call has at most three repair opportunities.
SQLite execution errors are feedback; the compiler does not impose a business
schema, one-table-per-file format, SQL operation whitelist or mandatory seed checks.

Both phases may read and revise database SQL files under database_source_directory.
Prefer schema/<entity>.sql and seed.sql. Keep reusable DDL in schema files, insertion
SQL in seed.sql, and do not hide initialization in unconsumed SQL files. If seed
mapping reveals a missing field/table/relation, edit its existing DDL in the SAME
batch as the inserts. Do not create a parallel entity just to avoid revising a file.
Use the exact snapshots for edit_file; delete/add may replace a draft file.

Schema phase: traverse every requirement/scenario in the supplied subtree,
including preconditions, outcomes, data.requires, resolved_data and interactions.
Infer entities, attributes, identifiers, ownership, cardinality, lifecycle/state,
uniqueness, relations and reload/persistence needs from behavior. Reuse accumulated
entities across subtrees. referenced_seed_field_shapes provides field names/types
without ROOT seed values; use these hints with the consuming behavior. ROOT itself
is not a schema-analysis subtree. Preserve behavior of earlier subtrees when
refining definitions; do not freeze a mistaken draft definition as immutable.
A session/search selection may be ephemeral; persist only what its behavior needs.
Prefer explicit foreign-key targets and useful indexes. Usually write CREATE TABLE
IF NOT EXISTS / CREATE INDEX IF NOT EXISTS for repeatable initialization. Related
tables may share a file. Views/triggers/index expressions are valid SQLite if useful.
Inspect existing definitions first; use their actual names and conventions.

Seeds phase: read ROOT data, descriptions and completed DDL; extract ALL declared
records, nested dictionaries/lists, owners, relationships, empty-state conventions,
status/amount/time fields and prerequisites. Preserve explicit values and historical
timestamps. A SEED declaration can describe a code, validation example or configuration
instead of a persisted business record: represent it in an appropriate configuration
row only when the application needs persistence; do not create accounts/travelers
merely from verification phones or identity-validation examples. CREATED is produced
by runtime/UI flows; DERIVED is produced by actions. Neither is an initial fixture.
Infer only technical IDs, missing parent containers or mandatory defaults needed
to represent explicit seeds, consistently with existing application conventions.
Do not guess extra inventory/history or populate explicitly empty owner lists.
Reuse owner keys across account, order and item inserts. Distinguish identity by
route/date/owner where required, rather than merging records by a display name.
Use parent-first, repeatable inserts with real keys or correct NOT EXISTS predicates.
Prefer ON CONFLICT ... DO NOTHING to preserve existing user data. If an existing
bootstrap row genuinely conflicts with explicit requirements, make a targeted
correction using its stable identity; avoid broad updates/deletes or REPLACE.
CREATE IF NOT EXISTS does not migrate existing columns/constraints: account for
the supplied existing_database_schema. For a live-schema change, provide a repeatable
migration consistent with fresh installation; do not silently edit CREATE alone.
SQL files are loaded in lexical path order, then seed.sql; preserve that order and
the transaction provided by initialization. Do not add redundant transaction wrappers.

seed_checks.json is OPTIONAL diagnostic evidence, not an acceptance gate.
When useful, add [{data_id,query,min_rows}] SELECT checks for actual declared rows.
Multiple queries may describe one data_id. No query is required for non-row examples.
Checks do not prove semantic completeness: privately compare every ROOT obligation
with the chosen SQL representation, including values/relationships and required
absence of rows, before returning the final batch.

Integration: the application initializes its existing shared connection before
queries and reads the SQL files through its bootstrap. Reuse existing database
index/db_runtime exports (run/get/all/withTransaction in web projects); use parameterized
queries and actual SQL column names in requirement-owned DB functions, then call them
from FUNC and API. Preserve ARC_DB_FILE isolation and transaction behavior; never
copy seed arrays into frontend code or seed on each request. Password seeds must use
the same credential encoding/comparison convention as authentication. Configured
verification codes become valid only after the declared send/verification flow.
Later source edits must keep startup and the schema/seed snapshot consistent.

Examples (replace paths and code with actual values):
[{"tool":"add_file","path":"<database>/schema/label.sql","content":"CREATE TABLE IF NOT EXISTS label (id TEXT PRIMARY KEY, name TEXT NOT NULL);\\n"}]
[{"tool":"edit_file","path":"<existing SQL file>","old_text":"<unique exact source>","new_text":"<correct SQL>"}]
[{"tool":"add_file","path":"<database>/seed.sql","content":"INSERT INTO label (id,name) VALUES ('reminders','Reminders') ON CONFLICT(id) DO NOTHING;\\n"}]
Escape quotes/newlines/backslashes in JSON content. No prose, schema report or
wrapper object; [] is valid when no source changes are needed.
"""


def statements(source: str) -> list[str]:
    """SQLite-aware splitting, including semicolons inside literals/comments."""
    result, buffer = [], ""
    for char in source:
        buffer += char
        if char == ";" and sqlite3.complete_statement(buffer):
            if _bare(buffer):
                result.append(buffer.strip())
            buffer = ""
    if _bare(buffer):
        if not sqlite3.complete_statement(buffer + ";"):
            raise ValueError("Incomplete SQL statement or unterminated quote/comment")
        result.append(buffer.strip())
    return result


def _bare(sql: str) -> str:
    # Only strip leading comments to inspect the statement's operation.
    return re.sub(r"\A(?:\s|--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/)*", "", sql).strip()


def identifier(value):
    """Quote SQLite metadata names without imposing a naming convention."""
    return '"' + str(value).replace('"', '""') + '"'


def _schema(files: dict[str, str], req_ids: list[str], *, seed_path: str = ""):
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys = ON")
    table_sources = {}
    try:
        for path, source in sorted(files.items()):
            if not path.endswith(".sql") or path == seed_path:
                continue
            batch = statements(source)
            for sql in batch:
                db.execute(sql)
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                table_sources.setdefault(name, path)
        tables = []
        for name, create in db.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            info = db.execute(f"PRAGMA table_info({identifier(name)})").fetchall()
            refs = db.execute(f"PRAGMA foreign_key_list({identifier(name)})").fetchall()
            columns = []
            for _, col, kind, nonnull, default, pk in info:
                ref = next((item for item in refs if item[3] == col), None)
                try:
                    value = db.execute(f"SELECT {default}").fetchone()[0] if default is not None else None
                except sqlite3.Error:
                    value = None  # default_sql remains the authoritative expression.
                columns.append({"name": col, "type": kind or "TEXT", "nullable": not bool(nonnull or pk),
                                "default": value, "references": {"table": ref[2], "column": ref[4], "on_delete": ref[6]} if ref else None,
                                "default_sql": default})
            primary = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            unique, indexes = [], []
            for index in db.execute(f"PRAGMA index_list({identifier(name)})").fetchall():
                key = [row[2] for row in db.execute(f"PRAGMA index_info({identifier(index[1])})")]
                if index[3] == "u":
                    unique.append(key)
                elif index[3] == "c":
                    indexes.append({"name": index[1], "columns": key, "unique": bool(index[2]),
                                    "sql": db.execute("SELECT sql FROM sqlite_master WHERE name=?", (index[1],)).fetchone()[0]})
            tables.append({"name": name, "req_ids": req_ids, "columns": columns,
                           "primary_key": primary, "unique": unique, "indexes": indexes,
                           "create_sql": create, "sql_file": table_sources.get(name, "")})
        return db, {"tables": tables, "seeds": []}
    except Exception:
        db.close()
        raise


def _edit_candidate(files, payload, allowed):
    result = dict(files)
    for call in payload.get("actions", []):
        path = call.get("path", "")
        if not allowed(path):
            raise ValueError(f"SQL task cannot edit path: {path}")
        tool = call["tool"]
        if tool == "add_file":
            if path in result:
                raise ValueError(f"{path} exists; use edit_file or delete_file then add_file")
            result[path] = call["content"]
        elif tool == "delete_file":
            if path not in result:
                raise ValueError(f"Cannot delete missing SQL file: {path}")
            del result[path]
        elif tool == "edit_file":
            if path not in result or not call["old_text"] or result[path].count(call["old_text"]) != 1:
                raise ValueError(f"edit_file requires one exact unique old_text in {path}")
            result[path] = result[path].replace(call["old_text"], call["new_text"], 1)
        else:
            raise ValueError(f"Unavailable SQL file tool: {tool}")
    return result


def _data_shape(value):
    """Expose declaration fields to DDL analysis without supplying seed values."""
    if isinstance(value, dict):
        return {key: _data_shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_data_shape(item) for item in value]
    return type(value).__name__


async def _generate(preparation, model, phase, context, files, allowed, validate, *, soft=False):
    schema = SQLFiles.model_json_schema()
    contract = tool_contract(schema)
    contract["tools"] = {name: spec for name, spec in contract["tools"].items()
                         if name in {"add_file", "edit_file", "delete_file"}}
    candidate, error, best = dict(files), "", None
    if soft:
        try:
            best = (dict(candidate), validate(candidate, coverage=False))
        except (ValueError, KeyError, TypeError, sqlite3.Error):
            pass
    for attempt in range(4):  # Initial call plus exactly three repair opportunities.
        await preparation._log(f"{phase}: {'generation' if attempt == 0 else f'repair {attempt}/3'}")
        try:
            content = await generate_text(model, [
                {"role": "system", "content": SQL_PROMPT},
                {"role": "user", "content": format_task_input({
                    "phase": phase, "app_type": preparation.app_type, **context,
                    "feedback": error, "sources": candidate,
                }, contract)},
            ], stage=f"DATABASE_SQL_{phase}_{attempt}", workspace_root=str(preparation.workspace))
            payload = parse_tool_sequence(content, schema)
            SQLFiles.model_validate(payload)
            candidate = _edit_candidate(candidate, payload, allowed)
            value = validate(candidate)
            return candidate, value, []
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            error = str(exc)
            await preparation._log(f"{phase}: rejected candidate: {error}")
            if soft:
                # Keep only executable candidates; uncovered data may continue.
                try:
                    best = (dict(candidate), validate(candidate, coverage=False))
                except (ValueError, KeyError, TypeError, sqlite3.Error):
                    pass
    write_json_file(preparation.workspace / f".arc/database/{phase}_failure.json",
                    {"error": error, "candidate_files": candidate, "repair_attempts": 3})
    if not soft:
        raise ValueError(f"{phase} failed after initial generation and 3 repairs: {error}")
    if best is not None:
        return *best, [{"phase": "seeds", "error": error}]
    # Keep the schema/source baseline when no executable seed candidate survives.
    return dict(files), validate(files, coverage=False), [{"phase": "seeds", "error": error}]


def _seed(db, files, seed_path):
    """Execute the model's insertion SQL; SQLite failures become repair feedback."""
    inserts = statements(files.get(seed_path, ""))
    for sql in inserts:
        db.execute(sql).fetchall()
    db.commit()
    return inserts


def _coverage(db, files, checks_path, declarations):
    """Optional evidence, never a prerequisite for accepting executable SQL."""
    statuses = {entry["id"]: {"status": "UNVERIFIED", "error": "No optional coverage query supplied"}
                for entry in declarations}
    try:
        checks = json.loads(files.get(checks_path, "[]"))
    except (ValueError, TypeError) as exc:
        return {data_id: {"status": "UNVERIFIED", "error": str(exc)} for data_id in statuses}
    if not isinstance(checks, list):
        return statuses
    for check in checks:
        if not isinstance(check, dict) or check.get("data_id") not in statuses:
            continue
        data_id = check["data_id"]
        reads = []
        def authorize(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_READ:
                reads.append(arg1)
            return sqlite3.SQLITE_OK if action in {
                sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION
            } else sqlite3.SQLITE_DENY
        db.set_authorizer(authorize)
        try:
            minimum = max(1, int(check.get("min_rows", 1)))
            count = len(db.execute(check.get("query", "")).fetchmany(minimum))
            prior = statuses[data_id]
            matched = count >= minimum
            statuses[data_id] = {
                "status": "MATERIALIZED" if matched or prior["status"] == "MATERIALIZED" else "MISSING",
                "query": check.get("query", ""), "min_rows": minimum,
                "tables": sorted(set(reads) | set(prior.get("tables", []))),
            }
        except (ValueError, TypeError, sqlite3.Error) as exc:
            if statuses[data_id]["status"] != "MATERIALIZED":
                statuses[data_id] = {"status": "UNVERIFIED", "error": str(exc)}
        finally:
            db.set_authorizer(None)
    return statuses


def _program(db, plan, inserts, schema_files=None):
    tables = []
    for table in plan["tables"]:
        additions = {}
        for col in table["columns"]:
            sql = f"{identifier(col['name'])} {col['type']}"
            if not col["nullable"]:
                sql += " NOT NULL"
            if col["default_sql"] is not None:
                sql += " DEFAULT " + col["default_sql"]
            if col["references"]:
                ref = col["references"]
                target = f" ({identifier(ref['column'])})" if ref['column'] else ""
                sql += f" REFERENCES {identifier(ref['table'])}{target} ON DELETE {ref['on_delete']}"
            additions[col["name"]] = f"ALTER TABLE {identifier(table['name'])} ADD COLUMN {sql}"
        create = re.sub(r"^CREATE TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?", "CREATE TABLE IF NOT EXISTS ", table["create_sql"], count=1, flags=re.I)
        tables.append({"name": table["name"], "columns": table["columns"], "create": create, "add_columns": additions})
    indexes = [re.sub(r"^(CREATE\s+(?:UNIQUE\s+)?INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?", r"\1 IF NOT EXISTS ", sql, count=1, flags=re.I)
               for (sql,) in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name")]
    return {"tables": tables, "indexes": indexes, "seeds": inserts,
            "schema_sql": [sql for path, source in sorted((schema_files or {}).items())
                           if path.endswith('.sql')
                           for sql in statements(source)]}


def _apply_program(db, program):
    db.execute("PRAGMA foreign_keys = ON")
    with db:
        db.execute("BEGIN IMMEDIATE")
        for sql in program.get("schema_sql", []):
            db.execute(sql)
        # SQL sources are authoritative: do not replay reconstructed DDL/migrations.
        for table in ([] if "schema_sql" in program else program["tables"]):
            db.execute(table["create"])
            existing = {row[1] for row in db.execute(f"PRAGMA table_info({identifier(table['name'])})")}
            for col in table["columns"]:
                if col["name"] not in existing:
                    db.execute(table["add_columns"][col["name"]])
        for sql in ([] if "schema_sql" in program else program["indexes"]) + program["seeds"]:
            db.execute(sql)

async def prepare_sql_database(preparation, tree, revision, runtime, *, commit=True, force=False):
    from arcbench_agent_runtime.requirement_contracts import resolve_requirement_contracts
    from core.database import application_db_path, inspect_database, plan_changes
    root_path = preparation.workspace
    state = read_json_file(preparation.state_path) or {}
    previous = state.get("applied_plan") or {"tables": [], "seeds": []}
    os.environ["ARC_DATABASE_READY"] = "0"
    state.update({"status": "RUNNING", "error": "", "mode": "sql_files"})
    write_json_file(preparation.state_path, state)
    try:
        tree = resolve_requirement_contracts(tree)
        root = {key: value for key, value in tree.items() if key != "children"}
        declarations = [item for item in root.get("data", []) if isinstance(item, dict) and item.get("lifecycle") == "SEED"] if isinstance(root.get("data"), list) else []
        ids = sorted({str(node["id"]) for node in _nodes(tree)})
        base = {"web": "backend/src/database", "cli": "app/database", "android": "app/src/main/assets/database"}[preparation.app_type]
        db_path = application_db_path(root_path, preparation.app_type)
        schema_dir = base + "/schema/"
        seed_path, checks_path = base + "/seed.sql", base + "/seed_checks.json"
        def sql_source_path(path):
            target = root_path / path
            return (not Path(path).is_absolute() and target.resolve().is_relative_to(root_path.resolve())
                    and target.resolve().is_relative_to((root_path / base).resolve())
                    and (path.endswith('.sql') or path == checks_path))
        analysis_path = root_path / ".arc/database/sql_analysis.json"
        existing_sql_files = {}
        for path in (root_path / base).rglob('*.sql'):
            if path.is_file():
                relative = path.relative_to(root_path).as_posix()
                if not sql_source_path(relative):
                    raise ValueError(f"Database SQL path escapes workspace: {relative}")
                existing_sql_files[relative] = path.read_text(encoding='utf-8')
        checkpoint = read_json_file(analysis_path) or {}
        key = hashlib.sha256(f"{SQL_ANALYSIS_VERSION}:{revision}".encode()).hexdigest()
        if force or checkpoint.get("key") != key:
            checkpoint = {"key": key, "batches": [], "schema_files": {}, "seed_files": {}, "complete": False}
            # Preserve applied schema when evolving an existing application.
            checkpoint["schema_files"] = {path: source for path, source in existing_sql_files.items()
                                          if path != seed_path}
            checkpoint["seed_files"] = {path: (root_path / path).read_text(encoding="utf-8")
                                        for path in (seed_path, checks_path) if (root_path / path).is_file()}
            if not checkpoint["schema_files"] and db_path.exists():
                # Upgrade existing record-based workspaces without replacing data.
                with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as existing:
                    for name, spec in inspect_database(db_path).items():
                        ddl = re.sub(r"^CREATE TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?", "CREATE TABLE IF NOT EXISTS ", spec["sql"], count=1, flags=re.I)
                        indexes = [re.sub(r"^(CREATE\s+(?:UNIQUE\s+)?INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?", r"\1 IF NOT EXISTS ", sql, count=1, flags=re.I)
                                   for (sql,) in existing.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (name,))]
                        checkpoint["schema_files"][schema_dir + name + ".sql"] = ddl + ";\n" + ";\n".join(indexes) + (";\n" if indexes else "")
        disk_schema = {path: source for path, source in existing_sql_files.items() if path != seed_path}
        if checkpoint.get('complete') and (
                disk_schema != checkpoint.get('schema_files', {}) or
                any(existing_sql_files.get(path) != checkpoint.get('seed_files', {}).get(path)
                    for path in (seed_path,) if path in existing_sql_files)):
            checkpoint.update({'schema_files': disk_schema, 'complete': False, 'issues': []})
        schema_files = checkpoint["schema_files"]
        model = create_arc_chat_model(os.environ.get("MODEL", "openai:gpt-5.4"))
        batches = tree.get("children") or []
        for index, subtree in enumerate(batches):
            if index < len(checkpoint["batches"]):
                continue
            await preparation._log(f"Creating SQL schema {index + 1}/{len(batches)}: {subtree['id']}")
            def validate_schema(candidate):
                db, plan = _schema(candidate, ids, seed_path=seed_path)
                db.close()
                return plan
            schema_files, _, _ = await _generate(preparation, model, f"schema_{subtree['id']}", {
                "requirements": _schema_batch(subtree, {item["id"] for item in declarations}),
                "referenced_seed_field_shapes": [
                    {"id": item["id"], "entity": item.get("entity"),
                     "field_shapes": _data_shape(item.get("properties", {}))}
                    for item in declarations if any(item["id"] == data.get("id")
                        for node in _nodes(subtree) for data in node.get("resolved_data", []))],
                "schema_directory": schema_dir, "complete_current_schema_files": schema_files,
                "database_source_directory": base, "existing_database_schema": inspect_database(db_path),
            }, schema_files, sql_source_path, validate_schema)
            checkpoint.setdefault('seed_files', {}).update({path: source for path, source in schema_files.items()
                                                           if path == seed_path or not path.endswith('.sql')})
            schema_files = {path: source for path, source in schema_files.items()
                            if path.endswith('.sql') and path != seed_path}
            checkpoint["schema_files"] = schema_files
            checkpoint["batches"].append(subtree["id"])
            write_json_file(analysis_path, checkpoint)
            write_generated_files(root_path, schema_files)
        schema_db, plan = _schema(schema_files, ids)
        schema_db.close()
        seed_files = {**{path: source for path, source in checkpoint.get("seed_files", {}).items()
                        if not path.endswith('.sql') or path == seed_path}, **schema_files}
        # SQL files are authoritative, even after node-level edits or manual repair.
        for relative in (seed_path, checks_path):
            if (root_path / relative).is_file():
                seed_files[relative] = (root_path / relative).read_text(encoding='utf-8')
        issues = checkpoint.get("issues", [])
        def validate_seeds(candidate, *, coverage=True):
            candidate_schema = {path: source for path, source in candidate.items()
                                if path != seed_path and path.endswith(".sql")}
            db, current = _schema(candidate_schema, ids)
            try:
                inserts = _seed(db, candidate, seed_path)
                statuses = _coverage(db, candidate, checks_path, declarations)
                program = _program(db, current, inserts, candidate_schema)
                if db_path.exists():
                    with sqlite3.connect(":memory:") as scratch:
                        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as source:
                            source.backup(scratch)
                        _apply_program(scratch, program)
                        statuses = _coverage(scratch, candidate, checks_path, declarations)
                for table in current["tables"]:
                    related_ids = [data_id for data_id, status in statuses.items()
                                   if table["name"] in status.get("tables", [])]
                    key = table["primary_key"] or next(iter(table["unique"]), [])
                    cursor = db.execute(f"SELECT * FROM {identifier(table['name'])}")
                    names = [col[0] for col in cursor.description]
                    rows = [dict(zip(names, row)) for row in cursor.fetchall()]
                    if rows:
                        current["seeds"].append({
                            "table": table["name"], "req_ids": [str(root["id"])],
                            "source": "Executed seed.sql", "conflict_columns": key,
                            "rows": rows, "data_ids": related_ids,
                        })
                return current, program, statuses
            finally:
                db.close()
        if not checkpoint.get("complete"):
            seed_files, _, issues = await _generate(preparation, model, "seeds", {
                "requirement": root, "complete_schema": plan["tables"], "schema_sql_files": schema_files,
                "existing_bootstrap_records": previous["seeds"],
                "schema_requirements": [_schema_batch(batch, {item["id"] for item in declarations})
                                        for batch in batches],
                "database_source_directory": base,
                "existing_database_schema": inspect_database(db_path),
                "seed_sql_path": seed_path, "seed_checks_path": checks_path,
            }, seed_files, sql_source_path, validate_seeds, soft=True)
            schema_files = {path: source for path, source in seed_files.items()
                            if path != seed_path and path.endswith('.sql')}
            checkpoint.update({"schema_files": schema_files, "seed_files": seed_files,
                               "issues": issues, "complete": True})
            write_json_file(analysis_path, checkpoint)
        plan, program, statuses = validate_seeds(seed_files, coverage=False)
        issues = list(issues)
        for data_id, entry in statuses.items():
            consumers = [node["id"] for node in _nodes(tree) if any(item["id"] == data_id for item in node.get("resolved_data", []))]
            entry["req_ids"] = consumers
            if entry["status"] != "MATERIALIZED":
                issues.append({"phase": "seeds", "data_id": data_id, "req_ids": consumers,
                               "error": entry.get("error", f"Optional SEED evidence: {entry['status']}"),
                               "advisory": True})
        # Existing live data is never replaced. Validate changes on a backup first.
        with sqlite3.connect(":memory:") as scratch:
            if db_path.exists():
                with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as source:
                    source.backup(scratch)
            _apply_program(scratch, program)
        if preparation.app_type == "web":
            files = fixed_web_runtime(root_path, sql_files=True)
        else:
            files = render_files(plan, preparation.app_type, get_android_package(), program=program)
            files.update(runtime_bootstrap_hook(root_path, preparation.app_type, get_android_package()))
        files.update(schema_files)
        files.update({seed_path: "-- No executable seed rows were accepted.\n", checks_path: "[]\n", **seed_files})
        for relative in set(existing_sql_files) - set(files):
            target = root_path / relative
            retired = root_path / '.arc/database/retired_sql' / key / relative
            retired.parent.mkdir(parents=True, exist_ok=True)
            target.replace(retired)
        write_generated_files(root_path, files)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as db:
            _apply_program(db, program)
        if preparation.app_type == "web":
            await preparation._prepare_web(db_path)
        state.update({"status": "COMPLETED", "error": "", "mode": "sql_files", "analysis_version": SQL_ANALYSIS_VERSION,
                      "requirements_revision": revision, "plan": plan, "applied_plan": plan,
                      "sql_program": program, "analysis_validated": True, "runtime_verified": True,
                      "generated_files": sorted(set(files) | (set(state.get("generated_files", [])) - {"backend/src/database/arc_database.js"})),
                      "database_path": str(db_path.relative_to(root_path)), "seed_data_status": statuses,
                      "skipped_records": issues, "blocked_node_ids": [], "degraded": bool(issues),
                      "affected_node_ids": sorted(set(state.get("affected_node_ids", [])) | set(plan_changes(previous, plan))),
                      "impact_revision": revision,
                      "code_hashes": {path: hashlib.sha256((root_path / path).read_bytes()).hexdigest() for path in files}})
        write_json_file(preparation.state_path, state)
        write_json_file(root_path / ".arc/database/skipped_records.json", {"records": issues})
        os.environ["ARC_DATABASE_READY"] = "1"
        runtime.events.notify_traceability_changed("database_prepared")
        preparation._register_interfaces(plan, state, runtime)
        if commit:
            runtime.git.commit("ARC DATABASE_PREPARE: validate SQL schema and bootstrap data")
        await preparation._log(f"SQL database ready; {len(issues)} seed issue(s), node compilation continues.")
        return state
    except Exception as exc:
        state.update({"status": "FAILED", "error": str(exc), "runtime_verified": False})
        write_json_file(preparation.state_path, state)
        await preparation._log(f"SQL database creation failed; compilation stopped: {exc}", "error")
        return state
