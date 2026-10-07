"""Small-database SQL generation: subtree DDL, one seed batch, bounded repairs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3

from agents.database_designer import _nodes, _schema_batch
from agents.model.factory import create_arc_chat_model
from agents.model.native_openai import generate_text
from agents.model.prompt_input import format_task_input
from agents.model.tool_sequence import parse_tool_sequence, tool_contract
from agents.runtime.plain_codegen import Record, AddFile, EditFile, DeleteAction, Replacement, NewFile, DeleteFile
from core.config import get_android_package
from core.database_codegen import fixed_web_runtime, render_files, runtime_bootstrap_hook, write_generated_files
from core.database_plan import identifier
from core.files import read_json_file, write_json_file
from pydantic import Field


SQL_ANALYSIS_VERSION = 7


class SQLFiles(Record):
    changes: list[Replacement] = Field(default_factory=list)
    actions: list[AddFile | EditFile | DeleteAction] = Field(default_factory=list)
    new_files: list[NewFile] = Field(default_factory=list)
    delete_files: list[DeleteFile] = Field(default_factory=list)


SQL_PROMPT = """Prepare a small SQLite application's database using SQL source files.
Return only a JSON array of edit_file/add_file/delete_file calls; arguments are
directly beside tool. No define_table/seed_rows, SQL prose, Markdown or reports.
The system supplies ALL current SQL files, schema and existing bootstrap records.
File actions edit a candidate, not the application database. Rejected candidates
are not applied; repair against the supplied candidate source snapshots.
Each task gets one generation call and at most THREE repair calls.

Schema phase: analyze only the supplied child subtree, never ROOT's startup data.
Create one schema/<table>.sql file per table, including that table's indexes.
Use ASCII table/column/index names. Write explicit target columns in REFERENCES.
Reuse earlier entities; preserve all previously required columns, keys, relations
and indexes. Use CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
No INSERT/UPDATE/DELETE, DROP, ALTER, PRAGMA, transactions, triggers or views.
To change a draft table, edit its CREATE definition; the validator rebuilds ALL
schema files on a fresh SQLite database. Forward foreign keys may resolve in later
subtrees; every foreign key must resolve after the final subtree.
Do not invent application APIs or optional future modules. [] means no schema change.

Seeds phase: generate ONE complete seed.sql and seed_checks.json using ROOT data
and the FULL finalized schema. SEED initializes the product. CREATED/DERIVED must
not become startup rows. Preserve values/identities/timestamps/relationships.
You may initialize the minimal parent/default container required by an explicit
SEED (e.g. the personal workspace); reuse its identity, insert parents first, and
do not turn runtime-created accounts/orders/notes into fixtures.
Use idempotent INSERT with a real unique/primary key, ON CONFLICT ... DO NOTHING,
or INSERT ... SELECT ... WHERE NOT EXISTS. Never INSERT OR REPLACE, destructive
updates, schema changes, external files or transaction/PRAGMA statements.
For a title that is NOT unique, do not invent ON CONFLICT(title); use a stable
primary key or a correct NOT EXISTS predicate. SQL defaults may use valid SQLite
expressions. Explicit historical seed timestamps must remain unchanged.
seed_checks.json is a JSON ARRAY of {data_id,query,min_rows}. query is one SELECT
returning the actual matching records, not SELECT 1 or COUNT(*). Include the
declared identity AND essential values/ownership/state in its WHERE clause.
Cover every declared SEED data_id, all list records and required relationships.
min_rows is a positive integer, normally 1 or the declared record-list length.
[] is valid only if there are no explicit or legacy startup obligations.

Illustrative return shapes (replace paths/content with actual task values):
[{"tool":"add_file","path":"<schema directory>/label.sql","content":"CREATE TABLE IF NOT EXISTS label (label_id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL UNIQUE);\\n"}]
[{"tool":"edit_file","path":"<existing SQL path>","old_text":"<unique exact existing SQL>","new_text":"<correct SQL>"}]
To regenerate an existing file, return delete then add in the SAME batch:
[{"tool":"delete_file","path":"<existing SQL path>"},{"tool":"add_file","path":"<same SQL path>","content":"<complete replacement SQL>"}]
Seed example for a finalized label table (adapt to its real columns and parents):
[{"tool":"add_file","path":"<seed.sql path>","content":"INSERT INTO label (label_id, name) VALUES ('reminders', 'Reminders') ON CONFLICT(label_id) DO NOTHING;\\n"},{"tool":"add_file","path":"<seed_checks.json path>","content":"[{\\"data_id\\":\\"DATA-REMINDERS\\",\\"query\\":\\"SELECT * FROM label WHERE label_id='reminders' AND name='Reminders'\\",\\"min_rows\\":1}]"}]
Optional tools/fields in definitions are not output wrappers. Escape quotes,
newlines and backslashes inside JSON content strings. Never copy example fixtures.
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


def _schema(files: dict[str, str], req_ids: list[str], *, final: bool):
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys = ON")
    try:
        for path, source in sorted(files.items()):
            if not path.endswith(".sql"):
                continue
            batch = statements(source)
            creates = [sql for sql in batch if re.match(r"CREATE\s+TABLE\b", _bare(sql), re.I)]
            if len(creates) != 1:
                raise ValueError(f"{path}: each schema file must define exactly one table")
            table_name = path.rsplit("/", 1)[-1][:-4]
            if not re.match(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+[\"`\[]?" + re.escape(table_name) + r"[\"`\]]?\s*\(", _bare(creates[0]), re.I):
                raise ValueError(f"{path}: table name must match its SQL filename")
            for sql in batch:
                if not re.match(r"CREATE\s+(?:TABLE|(?:UNIQUE\s+)?INDEX)\s+IF\s+NOT\s+EXISTS\b", _bare(sql), re.I):
                    raise ValueError(f"{path}: schema accepts only CREATE TABLE/INDEX IF NOT EXISTS")
                db.execute(sql)
        tables = []
        for name, create in db.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            identifier(name)
            info = db.execute(f"PRAGMA table_info({identifier(name)})").fetchall()
            refs = db.execute(f"PRAGMA foreign_key_list({identifier(name)})").fetchall()
            columns = []
            for _, col, kind, nonnull, default, pk in info:
                identifier(col)
                ref = next((item for item in refs if item[3] == col), None)
                value = db.execute(f"SELECT {default}").fetchone()[0] if default is not None else None
                columns.append({"name": col, "type": kind or "TEXT", "nullable": not bool(nonnull or pk),
                                "default": value, "references": {"table": ref[2], "column": ref[4], "on_delete": ref[6]} if ref else None,
                                "default_sql": default})
            primary = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            unique, indexes = [], []
            for index in db.execute(f"PRAGMA index_list({identifier(name)})").fetchall():
                key = [row[2] for row in db.execute(f"PRAGMA index_info(\"{index[1]}\")")]
                if any(col is None for col in key):
                    raise ValueError("Expression indexes are not supported by the shared database contract")
                if index[3] == "u":
                    unique.append(key)
                elif index[3] == "c":
                    identifier(index[1])
                    if index[4]:
                        raise ValueError("Partial indexes are not supported by the shared database contract")
                    indexes.append({"name": index[1], "columns": key, "unique": bool(index[2])})
            tables.append({"name": name, "req_ids": req_ids, "columns": columns,
                           "primary_key": primary, "unique": unique, "indexes": indexes, "create_sql": create})
        if final:
            by_name = {table["name"]: table for table in tables}
            for table in tables:
                groups = {}
                for ref in db.execute(f"PRAGMA foreign_key_list({identifier(table['name'])})"):
                    groups.setdefault(ref[0], []).append(ref)
                for refs in groups.values():
                    refs.sort(key=lambda item: item[1])
                    target = by_name.get(refs[0][2])
                    key = [item[4] for item in refs]
                    if any(item is None for item in key):
                        raise ValueError(f"Foreign key in {table['name']} must explicitly name target columns")
                    keys = ([target["primary_key"], *target["unique"],
                             *[item["columns"] for item in target["indexes"] if item["unique"]]] if target else [])
                    if not target or not key or key not in keys:
                        raise ValueError(f"Invalid foreign key in {table['name']}: target={refs[0][2]}, columns={key}")
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


async def _generate(preparation, model, phase, context, files, allowed, validate, *, soft=False):
    schema = SQLFiles.model_json_schema()
    contract = tool_contract(schema)
    contract["tools"] = {name: spec for name, spec in contract["tools"].items()
                         if name in {"add_file", "edit_file", "delete_file"}}
    candidate, error, best = dict(files), "", None
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
    return {}, validate({}, coverage=False), [{"phase": "seeds", "error": error}]


def _seed(db, files, seed_path):
    seed_sql = files.get(seed_path, "")
    inserts = statements(seed_sql)
    for sql in inserts:
        if not re.match(r"INSERT\s+(?:(?:OR\s+IGNORE)\s+)?INTO\b", _bare(sql), re.I):
            raise ValueError("seed.sql accepts only INSERT/INSERT OR IGNORE; no REPLACE, DDL or updates")
        if re.search(r"\bDO\s+UPDATE\b", sql, re.I):
            raise ValueError("Seed upserts cannot update existing product data; use DO NOTHING")
        db.execute(sql).fetchall()
    db.commit()
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("SEED foreign key violations")
    first = _rows(db)
    for sql in inserts:
        db.execute(sql).fetchall()
    db.commit()
    if _rows(db) != first:
        raise ValueError("seed.sql is not idempotent; rerunning it changes bootstrap rows")
    return inserts


def _rows(db):
    return {name: sorted(db.execute(f"SELECT * FROM {identifier(name)}").fetchall(), key=repr)
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}


def _coverage(db, files, checks_path, declarations):
    checks = json.loads(files.get(checks_path, "[]"))
    if not isinstance(checks, list):
        raise ValueError("seed_checks.json must be an array")
    statuses = {}
    expected = {entry["id"]: entry for entry in declarations}
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"data_id", "query", "min_rows"}:
            raise ValueError("Each seed check needs only data_id, query, min_rows")
        data_id = check["data_id"]
        if data_id not in expected or data_id in statuses:
            raise ValueError(f"Unknown/duplicate SEED check: {data_id}")
        query, minimum = check["query"], check["min_rows"]
        if type(minimum) is not int or minimum < 1:
            raise ValueError(f"Invalid min_rows: {data_id}")
        props = expected[data_id].get("properties")
        if isinstance(props, list) and minimum < len(props):
            raise ValueError(f"SEED {data_id} requires at least {len(props)} declared records")
        if not isinstance(query, str) or not re.match(r"SELECT\b", _bare(query), re.I) or len(statements(query)) != 1:
            raise ValueError(f"SEED {data_id} requires one read-only SELECT")
        reads = []
        def authorize(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_READ:
                reads.append(arg1)
            return sqlite3.SQLITE_OK if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION} else sqlite3.SQLITE_DENY
        db.set_authorizer(authorize)
        try:
            count = len(db.execute(query).fetchmany(minimum))
        finally:
            db.set_authorizer(None)
        if not reads or re.search(r"\b(?:COUNT|SUM|MIN|MAX|AVG)\s*\(", query, re.I):
            raise ValueError(f"SEED {data_id} must select actual records, not constants/aggregate counts")
        statuses[data_id] = {"status": "MATERIALIZED" if count >= minimum else "MISSING",
                             "query": query, "min_rows": minimum, "tables": sorted(set(reads))}
    for data_id in expected:
        statuses.setdefault(data_id, {"status": "MISSING", "error": "No coverage query supplied"})
    return statuses


def _program(db, plan, inserts):
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
                sql += f" REFERENCES {identifier(ref['table'])} ({identifier(ref['column'])}) ON DELETE {ref['on_delete']}"
            additions[col["name"]] = f"ALTER TABLE {identifier(table['name'])} ADD COLUMN {sql}"
        create = re.sub(r"^CREATE TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?", "CREATE TABLE IF NOT EXISTS ", table["create_sql"], count=1, flags=re.I)
        tables.append({"name": table["name"], "columns": table["columns"], "create": create, "add_columns": additions})
    indexes = [re.sub(r"^(CREATE\s+(?:UNIQUE\s+)?INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?", r"\1 IF NOT EXISTS ", sql, count=1, flags=re.I)
               for (sql,) in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name")]
    return {"tables": tables, "indexes": indexes, "seeds": inserts}


def _apply_program(db, program):
    db.execute("PRAGMA foreign_keys = ON")
    with db:
        db.execute("BEGIN IMMEDIATE")
        for table in program["tables"]:
            db.execute(table["create"])
            existing = {row[1] for row in db.execute(f"PRAGMA table_info({identifier(table['name'])})")}
            for col in table["columns"]:
                if col["name"] not in existing:
                    if col["default_sql"] is None and not col["nullable"]:
                        raise ValueError(f"Unsafe existing-database column addition: {table['name']}.{col['name']}")
                    db.execute(table["add_columns"][col["name"]])
        for sql in program["indexes"] + program["seeds"]:
            db.execute(sql)
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Database foreign key violations")


def _check_live(db, plan):
    """Reject schema changes that CREATE IF NOT EXISTS would silently ignore."""
    for table in plan["tables"]:
        actual = db.execute(f"PRAGMA table_info({identifier(table['name'])})").fetchall()
        columns = {row[1]: row for row in actual}
        for col in table["columns"]:
            row = columns.get(col["name"])
            if (row is None or row[2].upper() != col["type"].upper() or row[4] != col["default_sql"]
                    or bool(row[3] or row[5]) != (not col["nullable"])):
                raise ValueError(f"SQL schema conflicts with live column {table['name']}.{col['name']}")
        primary = [row[1] for row in sorted(actual, key=lambda row: row[5]) if row[5]]
        if primary != table["primary_key"]:
            raise ValueError(f"SQL schema conflicts with live primary key: {table['name']}")
        indexes = {}
        for index in db.execute(f"PRAGMA index_list({identifier(table['name'])})").fetchall():
            key = [row[2] for row in db.execute(f"PRAGMA index_info(\"{index[1]}\")")]
            indexes[index[1]] = (key, bool(index[2]))
        if any((key, True) not in indexes.values() for key in table["unique"]):
            raise ValueError(f"SQL schema conflicts with live unique keys: {table['name']}")
        for index in table["indexes"]:
            if indexes.get(index["name"]) != (index["columns"], index["unique"]):
                raise ValueError(f"SQL schema conflicts with live index: {index['name']}")
        refs = db.execute(f"PRAGMA foreign_key_list({identifier(table['name'])})").fetchall()
        for col in table["columns"]:
            ref = col["references"]
            if ref and not any(row[3] == col["name"] and row[2] == ref["table"] and row[4] == ref["column"] and row[6] == ref["on_delete"] for row in refs):
                raise ValueError(f"SQL schema conflicts with live foreign key: {table['name']}.{col['name']}")


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
        analysis_path = root_path / ".arc/database/sql_analysis.json"
        checkpoint = read_json_file(analysis_path) or {}
        key = hashlib.sha256(f"{SQL_ANALYSIS_VERSION}:{revision}".encode()).hexdigest()
        if force or checkpoint.get("key") != key:
            checkpoint = {"key": key, "batches": [], "schema_files": {}, "seed_files": {}, "complete": False}
            # Preserve applied schema when evolving an existing application.
            checkpoint["schema_files"] = {path: (root_path / path).read_text(encoding="utf-8")
                                          for path in state.get("generated_files", [])
                                          if path.startswith(schema_dir) and (root_path / path).is_file()}
            if not checkpoint["schema_files"] and db_path.exists():
                # Upgrade existing record-based workspaces without replacing data.
                with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as existing:
                    for name, spec in inspect_database(db_path).items():
                        identifier(name)
                        ddl = re.sub(r"^CREATE TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?", "CREATE TABLE IF NOT EXISTS ", spec["sql"], count=1, flags=re.I)
                        indexes = [re.sub(r"^(CREATE\s+(?:UNIQUE\s+)?INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?", r"\1 IF NOT EXISTS ", sql, count=1, flags=re.I)
                                   for (sql,) in existing.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (name,))]
                        checkpoint["schema_files"][schema_dir + name + ".sql"] = ddl + ";\n" + ";\n".join(indexes) + (";\n" if indexes else "")
        schema_files = checkpoint["schema_files"]
        model = create_arc_chat_model(os.environ.get("MODEL", "openai:gpt-5.4"))
        batches = tree.get("children") or []
        for index, subtree in enumerate(batches):
            if index < len(checkpoint["batches"]):
                continue
            await preparation._log(f"Creating SQL schema {index + 1}/{len(batches)}: {subtree['id']}")
            def validate_schema(candidate):
                db, plan = _schema(candidate, ids, final=index == len(batches) - 1)
                try:
                    # Draft correction may add/change definitions but cannot lose
                    # previously required table/column names.
                    prior_db, prior = _schema(schema_files, ids, final=False)
                    prior_db.close()
                    current = {table["name"]: {col["name"] for col in table["columns"]} for table in plan["tables"]}
                    definitions = {table["name"]: table for table in plan["tables"]}
                    for table in prior["tables"]:
                        if not {col["name"] for col in table["columns"]} <= current.get(table["name"], set()):
                            raise ValueError(f"Cannot remove earlier required table/columns: {table['name']}")
                        updated = definitions[table["name"]]
                        if table["primary_key"] != updated["primary_key"] or any(key not in updated["unique"] for key in table["unique"]):
                            raise ValueError(f"Cannot remove earlier required keys: {table['name']}")
                        new_columns = {col["name"]: col for col in updated["columns"]}
                        for col in table["columns"]:
                            if col["references"] and col["references"] != new_columns[col["name"]]["references"]:
                                raise ValueError(f"Cannot remove/change earlier required foreign key: {table['name']}.{col['name']}")
                    if db_path.exists() and index == len(batches) - 1:
                        with sqlite3.connect(":memory:") as scratch:
                            with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as source:
                                source.backup(scratch)
                            _apply_program(scratch, _program(db, plan, []))
                            _check_live(scratch, plan)
                    return plan
                finally:
                    db.close()
            schema_files, _, _ = await _generate(preparation, model, f"schema_{subtree['id']}", {
                "requirements": _schema_batch(subtree, {item["id"] for item in declarations}),
                "schema_directory": schema_dir, "complete_current_schema_files": schema_files,
            }, schema_files, lambda path: bool(re.fullmatch(re.escape(schema_dir) + r"[A-Za-z][A-Za-z0-9_]*\.sql", path)), validate_schema)
            checkpoint["schema_files"] = schema_files
            checkpoint["batches"].append(subtree["id"])
            write_json_file(analysis_path, checkpoint)
            write_generated_files(root_path, schema_files)
        schema_db, plan = _schema(schema_files, ids, final=True)
        schema_db.close()
        seed_files, issues = checkpoint.get("seed_files", {}), checkpoint.get("issues", [])
        def validate_seeds(candidate, *, coverage=True):
            db, current = _schema(schema_files, ids, final=True)
            try:
                inserts = _seed(db, candidate, seed_path)
                try:
                    statuses = _coverage(db, candidate, checks_path, declarations)
                except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                    if coverage:
                        raise
                    statuses = {item["id"]: {"status": "MISSING", "error": str(exc)} for item in declarations}
                missing = [data_id for data_id, item in statuses.items() if item["status"] == "MISSING"]
                if coverage and missing:
                    raise ValueError(f"SEED coverage missing: {missing}; create necessary parent rows and actual-record coverage queries")
                program = _program(db, current, inserts)
                if db_path.exists():
                    with sqlite3.connect(":memory:") as scratch:
                        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as source:
                            source.backup(scratch)
                        _apply_program(scratch, program)
                        _apply_program(scratch, program)
                        try:
                            live_statuses = _coverage(scratch, candidate, checks_path, declarations)
                        except (ValueError, KeyError, TypeError, sqlite3.Error):
                            if coverage:
                                raise
                        else:
                            statuses = live_statuses
                            missing = [data_id for data_id, item in statuses.items() if item["status"] == "MISSING"]
                            if coverage and missing:
                                raise ValueError(f"SEED missing in existing database: {missing}; existing rows must not be overwritten")
                for table in current["tables"]:
                    related_ids = [data_id for data_id, status in statuses.items() if table["name"] in status.get("tables", [])]
                    key = table["primary_key"] or next(iter(table["unique"]), [])
                    if key:
                        cursor = db.execute(f"SELECT * FROM {identifier(table['name'])}")
                        names = [col[0] for col in cursor.description]
                        rows = [dict(zip(names, row)) for row in cursor.fetchall()]
                        if rows:
                            current["seeds"].append({"table": table["name"], "req_ids": [str(root["id"])],
                                                     "source": "Executed seed.sql", "conflict_columns": key,
                                                     "rows": rows, "data_ids": related_ids})
                return current, program, statuses
            finally:
                db.close()
        if not checkpoint.get("complete"):
            seed_files, _, issues = await _generate(preparation, model, "seeds", {
                "requirement": root, "complete_schema": plan["tables"], "schema_sql_files": schema_files,
                "existing_bootstrap_records": previous["seeds"],
                "seed_sql_path": seed_path, "seed_checks_path": checks_path,
            }, seed_files, lambda path: path in {seed_path, checks_path}, validate_seeds, soft=True)
            checkpoint.update({"seed_files": seed_files, "issues": issues, "complete": True})
            write_json_file(analysis_path, checkpoint)
        plan, program, statuses = validate_seeds(seed_files, coverage=False)
        issues = list(issues)
        for data_id, entry in statuses.items():
            consumers = [node["id"] for node in _nodes(tree) if any(item["id"] == data_id for item in node.get("resolved_data", []))]
            entry["req_ids"] = consumers
            if entry["status"] == "MISSING":
                issues.append({"phase": "seeds", "data_id": data_id, "req_ids": consumers,
                               "error": f"Required SEED {data_id} missing after 3 repairs; compilation continues"})
        # Existing live data is never replaced. Validate changes on a backup first.
        with sqlite3.connect(":memory:") as scratch:
            if db_path.exists():
                with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as source:
                    source.backup(scratch)
            _apply_program(scratch, program)
            _apply_program(scratch, program)
            _check_live(scratch, plan)
        if preparation.app_type == "web":
            files = fixed_web_runtime(root_path, sql_files=True)
        else:
            files = render_files(plan, preparation.app_type, get_android_package(), program=program)
            files.update(runtime_bootstrap_hook(root_path, preparation.app_type, get_android_package()))
        files.update(schema_files)
        files.update({seed_path: "-- No executable seed rows were accepted.\n", checks_path: "[]\n", **seed_files})
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
