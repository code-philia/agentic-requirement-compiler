"""Validated JSON intermediate records and deterministic SQLite compilation."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


Scalar = str | int | float | bool | None


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ForeignKey(Record):
    table: str
    column: str
    on_delete: Literal["NO ACTION", "RESTRICT", "CASCADE", "SET NULL"] = "NO ACTION"


class Column(Record):
    name: str
    type: Literal["INTEGER", "REAL", "TEXT", "BLOB", "NUMERIC"]
    nullable: bool = True
    default: Scalar = None
    references: ForeignKey | None = None


class Index(Record):
    name: str
    columns: list[str]
    unique: bool = False


class Table(Record):
    name: str
    req_ids: list[str]
    columns: list[Column]
    primary_key: list[str] = Field(default_factory=list)
    unique: list[list[str]] = Field(default_factory=list)
    indexes: list[Index] = Field(default_factory=list)


class Seed(Record):
    table: str
    req_ids: list[str]
    source: str
    conflict_columns: list[str]
    rows: list[dict[str, Scalar]]


class DatabasePlan(Record):
    tables: list[Table]
    seeds: list[Seed]


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value) or value.lower().startswith("sqlite_"):
        raise ValueError(f"Invalid database identifier: {value!r}")
    return '"' + value + '"'


def literal(value: Scalar) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Database values must be finite")
        if isinstance(value, int) and not -(2**63) <= value < 2**63:
            raise ValueError("Database integers must fit SQLite's signed 64-bit range")
        return str(value)
    if "\x00" in value:
        raise ValueError("Database strings must not contain NUL")
    return "'" + value.replace("'", "''") + "'"


def validate_plan(
    payload: dict[str, Any], requirement_ids: set[str], *, defer_references: bool = False,
) -> dict[str, Any]:
    plan = DatabasePlan.model_validate(payload).model_dump()
    tables = {table["name"]: table for table in plan["tables"]}
    names = [table["name"].casefold() for table in plan["tables"]]
    if len(set(names)) != len(names):
        raise ValueError("Database table names must be unique (case-insensitive)")
    index_names: set[str] = set()
    for table in plan["tables"]:
        identifier(table["name"])
        _validate_sources(table["req_ids"], requirement_ids)
        columns = {column["name"]: column for column in table["columns"]}
        if not columns or len({name.casefold() for name in columns}) != len(table["columns"]):
            raise ValueError(f"Missing or duplicate columns in {table['name']}")
        for column in table["columns"]:
            identifier(column["name"])
            literal(column["default"])
            ref = column["references"]
            if ref:
                identifier(ref["table"])
                identifier(ref["column"])
                if ref["on_delete"] == "SET NULL" and not column["nullable"]:
                    raise ValueError("SET NULL foreign keys require nullable columns")
                target = tables.get(ref["table"])
                if defer_references:
                    continue
                if not target or ref["column"] not in {item["name"] for item in target["columns"]}:
                    raise ValueError(f"Unknown foreign key target: {ref}")
                if [ref["column"]] not in unique_keys(target):
                    raise ValueError(f"Foreign key target must be individually unique: {ref}")
                if ref["on_delete"] == "SET NULL" and not column["nullable"]:
                    raise ValueError("SET NULL foreign keys require nullable columns")
        for key in [table["primary_key"], *table["unique"]]:
            if key:
                _validate_columns(key, columns)
        if any(columns[name]["nullable"] for name in table["primary_key"]):
            raise ValueError(f"Primary key columns must be non-null: {table['name']}")
        for index in table["indexes"]:
            identifier(index["name"])
            if index["name"].casefold() in index_names or index["name"].casefold() in names:
                raise ValueError(f"Duplicate database index: {index['name']}")
            index_names.add(index["name"].casefold())
            _validate_columns(index["columns"], columns)
    identities: dict[tuple[str, tuple[str, ...], str], dict[str, Any]] = {}
    for seed in plan["seeds"]:
        table = tables.get(seed["table"])
        if not table:
            raise ValueError(f"Unknown seed table: {seed['table']}")
        _validate_sources(seed["req_ids"], requirement_ids)
        if not seed["source"].strip() or not seed["rows"]:
            raise ValueError("Seed records require source evidence and concrete rows")
        columns = {column["name"]: column for column in table["columns"]}
        _validate_columns(seed["conflict_columns"], columns)
        if seed["conflict_columns"] not in unique_keys(table):
            raise ValueError(f"Seed conflict columns must be a unique key: {seed['table']}")
        for row in seed["rows"]:
            if not row or set(row) - set(columns):
                raise ValueError(f"Invalid seed columns in {seed['table']}")
            for key in seed["conflict_columns"]:
                if row.get(key) is None:
                    raise ValueError("Seed identities must be explicit and non-null")
            for name, column in columns.items():
                if not column["nullable"] and row.get(name, column["default"]) is None:
                    raise ValueError(f"Missing required seed value: {seed['table']}.{name}")
            for value in row.values():
                literal(value)
            if any(columns[name]["type"] == "BLOB" for name in row):
                raise ValueError("JSON seed records cannot populate BLOB columns")
            identity = (seed["table"], tuple(seed["conflict_columns"]), json.dumps(
                [row[key] for key in seed["conflict_columns"]], ensure_ascii=False, sort_keys=True,
            ))
            if identity in identities and identities[identity] != row:
                raise ValueError(f"Conflicting seed rows for {identity}")
            identities[identity] = row
    # Canonical ordering makes generated code stable; seed order preserves dependencies.
    plan["tables"].sort(key=lambda table: table["name"])
    return plan


def _validate_sources(ids: list[str], known: set[str]) -> None:
    if not ids or set(ids) - known:
        raise ValueError(f"Database records require known source requirement ids: {ids}")


def _validate_columns(names: list[str], columns: dict[str, Any]) -> None:
    if not names or len(set(names)) != len(names) or set(names) - set(columns):
        raise ValueError(f"Invalid database key/index columns: {names}")


def unique_keys(table: dict[str, Any]) -> list[list[str]]:
    return [table["primary_key"], *table["unique"], *[
        index["columns"] for index in table["indexes"] if index["unique"]
    ]]


def column_sql(column: dict[str, Any]) -> str:
    sql = f"{identifier(column['name'])} {column['type']}"
    if not column["nullable"]:
        sql += " NOT NULL"
    if column["default"] is not None:
        sql += " DEFAULT " + literal(column["default"])
    if column["references"]:
        ref = column["references"]
        sql += f" REFERENCES {identifier(ref['table'])} ({identifier(ref['column'])}) ON DELETE {ref['on_delete']}"
    return sql


def compile_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Compile closed JSON records to SQL statements, never accept model-written SQL."""
    tables = []
    indexes = []
    seeds = []
    for table in plan["tables"]:
        definitions = [column_sql(column) for column in table["columns"]]
        if table["primary_key"]:
            definitions.append("PRIMARY KEY (" + ", ".join(map(identifier, table["primary_key"])) + ")")
        for key in table["unique"]:
            definitions.append("UNIQUE (" + ", ".join(map(identifier, key)) + ")")
        tables.append({
            "name": table["name"], "columns": table["columns"],
            "create": f"CREATE TABLE IF NOT EXISTS {identifier(table['name'])} ({', '.join(definitions)})",
            "add_columns": {column["name"]: f"ALTER TABLE {identifier(table['name'])} ADD COLUMN {column_sql(column)}"
                            for column in table["columns"]},
        })
        for index in table["indexes"]:
            indexes.append("CREATE " + ("UNIQUE " if index["unique"] else "")
                           + f"INDEX IF NOT EXISTS {identifier(index['name'])} ON {identifier(table['name'])}"
                           + " (" + ", ".join(map(identifier, index["columns"])) + ")")
    for seed in plan["seeds"]:
        for row in seed["rows"]:
            names = sorted(row)
            seeds.append(
                f"INSERT INTO {identifier(seed['table'])} ({', '.join(map(identifier, names))})"
                + " VALUES (" + ", ".join(literal(row[name]) for name in names) + ")"
                + " ON CONFLICT (" + ", ".join(map(identifier, seed["conflict_columns"])) + ") DO NOTHING"
            )
    return {"tables": tables, "indexes": indexes, "seeds": seeds}


def ensure_additive(previous: dict[str, Any], current: dict[str, Any]) -> None:
    old_tables = {table["name"]: table for table in previous.get("tables", [])}
    new_tables = {table["name"]: table for table in current["tables"]}
    for name, old in old_tables.items():
        new = new_tables.get(name)
        if not new:
            raise ValueError(f"Database preparation cannot drop table {name}")
        if old["primary_key"] != new["primary_key"] or old["unique"] != new["unique"]:
            raise ValueError(f"Database preparation cannot redefine keys on {name}")
        columns = {column["name"]: column for column in new["columns"]}
        for column in old["columns"]:
            if columns.get(column["name"]) != column:
                raise ValueError(f"Database preparation cannot remove or redefine {name}.{column['name']}")
        for index in old["indexes"]:
            if index not in new["indexes"]:
                raise ValueError(f"Database preparation cannot remove or redefine index {index['name']}")
        old_names = {column["name"] for column in old["columns"]}
        for column in new["columns"]:
            if column["name"] not in old_names:
                if column["name"] in new["primary_key"] or (not column["nullable"] and column["default"] is None):
                    raise ValueError(f"Unsafe additive column: {name}.{column['name']}")
                if column["references"] and column["default"] is not None:
                    raise ValueError("Adding a foreign key column with a non-null default requires an explicit migration")
    # Preserve the bootstrap contract; never quietly change an existing record's identity/value.
    for seed in previous.get("seeds", []):
        matches = [item for item in current["seeds"]
                   if item["table"] == seed["table"] and item["conflict_columns"] == seed["conflict_columns"]]
        rows = [row for item in matches for row in item["rows"]]
        if any(row not in rows for row in seed["rows"]):
            raise ValueError(f"Database preparation cannot remove or redefine seeds for {seed['table']}")
