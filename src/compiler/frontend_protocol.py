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


def check_references(raw: dict[str, Any], existing: set[str], editable: set[str]) -> list[ShapeError]:
    errors: list[ShapeError] = []
    if not isinstance(raw, dict):
        return errors
    aliases: set[str] = set()
    creates = raw.get("creates", {})
    for table, rows in (creates.items() if isinstance(creates, dict) else []):
        if not isinstance(rows, list):
            continue
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("key"), str):
                continue
            key = row["key"]
            if not key or key != key.strip() or "@" in key or key in aliases or key in existing:
                errors.append(ShapeError(f"$.creates.{table}[{i}].key", "Empty, duplicate or reserved local key", key))
            aliases.add(key)
    components = raw.get("components", [])
    for i, row in enumerate(components if isinstance(components, list) else []):
        if not isinstance(row, dict) or not isinstance(row.get("key"), str):
            continue
        keys = [row["key"], row["key"] + ".root"]
        if row.get("ui_ids"):
            keys.append(row["key"] + ".use")
        for key in keys:
            if not row["key"] or key != key.strip() or "@" in key or key in aliases or key in existing:
                errors.append(ShapeError(f"$.components[{i}].key", "Empty, duplicate or reserved local key", key))
            aliases.add(key)

    def ref(value: Any, path: str) -> None:
        if isinstance(value, list):
            for i, child in enumerate(value):
                ref(child, f"{path}[{i}]")
        elif value is not None:
            if not isinstance(value, dict) or set(value) not in ({"id"}, {"local"}):
                errors.append(ShapeError(path, "Expected {id: string} or {local: string}", value))
                return
            if not isinstance(next(iter(value.values())), str):
                return
            valid = value["id"] in existing if "id" in value else value["local"] in aliases
            if not valid:
                errors.append(ShapeError(path, "Unknown ID or undeclared local key", value))

    def walk(value: Any, path: str) -> None:
        if isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, f"{path}[{i}]")
        elif isinstance(value, dict):
            if value.get("kind") == "LITERAL":
                return
            for key, child in value.items():
                if key in REF_FIELDS or key in {"existing_component_id", "ui_ids", "property_ids", "data_ids", "associations"}:
                    ref(child, f"{path}.{key}")
                else:
                    walk(child, f"{path}.{key}")
    walk(raw, "$")
    for i, item in enumerate(components if isinstance(components, list) else []):
        if not isinstance(item, dict):
            continue
        for field in ("existing_component_id", "ui_ids", "property_ids", "data_ids"):
            values = item.get(field)
            for j, value in enumerate(values if isinstance(values, list) else [values]):
                eid = value.get("id") if isinstance(value, dict) else None
                if isinstance(eid, str) and eid in existing and eid not in editable:
                    path = f"$.components[{i}].{field}" + (f"[{j}]" if isinstance(values, list) else "")
                    errors.append(ShapeError(path, "Assembly target is outside editable_ids", value))
    updates = raw.get("updates", [])
    for i, update in enumerate(updates if isinstance(updates, list) else []):
        record = update.get("record", {}) if isinstance(update, dict) else {}
        eid = record.get("id") if isinstance(record, dict) else None
        if not isinstance(eid, str):
            continue
        path = f"$.updates[{i}].record.id"
        ref({"id": eid}, path)
        if eid in existing and eid not in editable:
            errors.append(ShapeError(path, "Existing entity is referenceable but outside editable_ids", eid))
    return errors


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
For /associations return the complete reference array; for /requirement_mode return DESIGN or SUMMARY.
Preserve every unrelated field and decision. Never regenerate another requirement or copy examples.
Fix the reported error and equivalent errors in supplied records. At most 32 repairs.
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
    selected = set()
    for field in ("associations", "requirement_mode"):
        if field in candidate:
            records[f"/{field}"] = candidate[field]
    for issue in feedback.get("errors", [feedback]):
        match = re.match(r"^\$\.(creates\.([a-z]+)|updates|components)\[(\d+)\]", issue.get("path", ""))
        if match:
            selected.add("/" + match[1].replace(".", "/") + "/" + match[3])
        for field in ("associations", "requirement_mode"):
            if issue.get("path", "").startswith("$." + field):
                selected.add("/" + field)
    if selected:
        records = {key: value for key, value in records.items() if key in selected}
    return {
        "error": {**feedback, "hint": "Repair only supplied records, or append the missing declaration. Return repairs, never a complete patch."},
        "editable_records": records,
        "append_paths": [f"/creates/{table}/-" for table in candidate.get("creates", {})],
        "local_symbols": [{"key": row.get("key"), "table": table, "name": row.get("name"),
                           "component_id": row.get("component_id"), "type": row.get("type")}
                          for table, rows in candidate.get("creates", {}).items() for row in rows],
        "editable_ids": context.get("editable_ids", []),
        "component_decisions": candidate.get("components", []),
        "existing_entities": [{k: row[k] for k in ("id", "name", "kind", "component_id", "owner_id", "type") if k in row}
                              for row in context.get("entities", [])],
        "api_contracts": context.get("api_contracts", []),
    }


def apply_repairs(candidate: dict[str, Any], response: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not 1 <= len(response["repairs"]) <= 32:
        raise ValueError("Return 1 to 32 local record repairs")
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
        if path in {"/associations", "/requirement_mode"}:
            result[path[1:]] = value
            continue
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
