"""Canonical model projection, explicit linking and bounded record repairs."""
from __future__ import annotations

import copy
import re
from typing import Any

from .frontend_generation_contracts import ShapeError, DEFS
from .frontend_workspace import REF_FIELDS


def project_context(value: Any) -> Any:
    """Project IR reference fields; never descend into literal business payloads."""
    if isinstance(value, list):
        return [project_context(v) for v in value]
    if not isinstance(value, dict):
        return value
    if value.get("kind") == "LITERAL":
        return copy.deepcopy(value)
    result = {}
    for key, child in value.items():
        if key in REF_FIELDS or key == "default_component_id":
            result[key] = ([{"id": v} for v in child] if isinstance(child, list)
                           else {"id": child} if child is not None else None)
        else:
            result[key] = project_context(child)
    return result


def check_references(raw: dict[str, Any], existing: set[str]) -> None:
    aliases: set[str] = set()
    for table, rows in raw.get("creates", {}).items():
        for i, row in enumerate(rows):
            key = row["key"]
            if not key or key != key.strip() or "@" in key or key in aliases or key in existing:
                raise ShapeError(f"$.creates.{table}[{i}].key", "Empty, duplicate or reserved local key", key)
            aliases.add(key)
    for i, row in enumerate(raw.get("components", [])):
        for key in (row["key"], row["key"] + ".root", row["key"] + ".use"):
            if not row["key"] or key != key.strip() or "@" in key or key in aliases or key in existing:
                raise ShapeError(f"$.components[{i}].key", "Empty, duplicate or reserved local key", key)
            aliases.add(key)

    def ref(value: Any, path: str) -> None:
        if isinstance(value, list):
            for i, child in enumerate(value):
                ref(child, f"{path}[{i}]")
        elif value is not None:
            if not isinstance(value, dict) or set(value) not in ({"id"}, {"local"}):
                raise ShapeError(path, "Expected {id: string} or {local: string}", value)
            valid = value["id"] in existing if "id" in value else value["local"] in aliases
            if not valid:
                raise ShapeError(path, "Unknown ID or undeclared local key", value)

    def walk(value: Any, path: str) -> None:
        if isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, f"{path}[{i}]")
        elif isinstance(value, dict):
            if value.get("kind") == "LITERAL":
                return
            for key, child in value.items():
                if key in REF_FIELDS or key in {"existing_component_id", "ui_ids", "property_ids", "data_ids"}:
                    ref(child, f"{path}.{key}")
                else:
                    walk(child, f"{path}.{key}")
    walk(raw, "$")
    for i, update in enumerate(raw.get("updates", [])):
        ref({"id": update["record"]["id"]}, f"$.updates[{i}].record.id")


REPAIR_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["repairs"],
    "properties": {"repairs": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["path", "value"],
        "properties": {"path": {"type": "string"}, "value": {"$ref": "#/$defs/json_value"}},
    }}},
}
REPAIR_SCHEMA["$defs"] = {"json_value": DEFS["json_value"]}
REPAIR_SCHEMA["x-arc-output-mode"] = "json_object"
REPAIR_INSTRUCTIONS = """Repair a frozen frontend design candidate, not the design itself.
Return only repairs [{path,value}]. Each path must be an exact supplied editable record path
or an allowed append path. value is the native replacement object, never a JSON-encoded string.
Preserve every unrelated field and decision. Never regenerate another requirement or copy examples.
Fix the reported error and equivalent errors in supplied records. At most 8 repairs.
References are {local: key} or {id: supplied_id}. Never use @ prefixes.
LITERAL expressions use native value; updates use {table,record} with the full final entity record.
Keep all kind-inactive fields as null/[]; never shorten a record. Do not invent business contracts or APIs.
"""


def repair_context(candidate: dict[str, Any], feedback: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    records = {}
    for table, rows in candidate.get("creates", {}).items():
        for i, row in enumerate(rows):
            records[f"/creates/{table}/{i}"] = row
    for table in ("updates", "components"):
        for i, row in enumerate(candidate.get(table, [])):
            records[f"/{table}/{i}"] = row
    match = re.match(r"^\$\.(creates\.([a-z]+)|updates|components)\[(\d+)\]", feedback.get("path", ""))
    if match:
        selected = "/" + match[1].replace(".", "/") + "/" + match[3]
        records = {selected: records[selected]} if selected in records else records
    return {
        "error": {**feedback, "hint": "Repair only supplied records, or append the missing declaration. Return repairs, never a complete patch."},
        "editable_records": records,
        "append_paths": [f"/creates/{table}/-" for table in candidate.get("creates", {})],
        "local_symbols": [{"key": row.get("key"), "table": table, "name": row.get("name"),
                           "component_id": row.get("component_id"), "type": row.get("type")}
                          for table, rows in candidate.get("creates", {}).items() for row in rows],
        "component_decisions": candidate.get("components", []),
        "existing_entities": [{k: row[k] for k in ("id", "name", "kind", "component_id", "owner_id", "type") if k in row}
                              for row in context.get("entities", [])],
        "api_contracts": context.get("api_contracts", []),
    }


def apply_repairs(candidate: dict[str, Any], response: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not 1 <= len(response["repairs"]) <= 8:
        raise ValueError("Return 1 to 8 local record repairs")
    result = copy.deepcopy(candidate)
    seen = set()
    for repair in response["repairs"]:
        path = repair["path"]
        if path not in context["editable_records"] and path not in context["append_paths"]:
            raise ValueError(f"Repair outside supplied scope: {path}")
        if path in seen and not path.endswith("/-"):
            raise ValueError(f"Repeated repair: {path}")
        seen.add(path)
        value = copy.deepcopy(repair["value"])
        if not isinstance(value, dict):
            raise ValueError("A repaired entity/update must be an object")
        parts = path.lstrip("/").split("/")
        parent = result
        for part in parts[:-1]:
            parent = parent[part]
        if parts[-1] == "-":
            parent.append(value)
        else:
            parent[int(parts[-1])] = value
    return result
