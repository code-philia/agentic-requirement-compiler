"""Canonical model projection and lightweight reference checks."""
from __future__ import annotations

import copy
from difflib import SequenceMatcher
from typing import Any

from .frontend_generation_contracts import ShapeError
from .frontend_workspace import REF_FIELDS


def check_request_targets(raw: Any, api_ids: set[str]) -> list[ShapeError]:
    """Validate external API identities on both new and updated Effect records."""
    if not isinstance(raw, dict):
        return []
    records = []
    creates = raw.get("creates")
    effects = creates.get("effects") if isinstance(creates, dict) else None
    if isinstance(effects, list):
        records.extend((f"$.creates.effects[{i}].target", row) for i, row in enumerate(effects))
    updates = raw.get("updates")
    if isinstance(updates, list):
        records.extend((f"$.updates[{i}].record.target", item.get("record"))
                       for i, item in enumerate(updates)
                       if isinstance(item, dict) and item.get("table") == "effects")
    errors = []
    for path, row in records:
        if not isinstance(row, dict) or row.get("kind") != "REQUEST":
            continue
        target = row.get("target")
        if isinstance(target, str) and target in api_ids:
            continue
        candidates = sorted(api_ids, key=lambda key: (
            -SequenceMatcher(None, str(target), key).ratio(), key))[:5]
        errors.append(ShapeError(
            path,
            "Unknown REQUEST API target. Copy an exact api_contracts.id, including its requirement prefix; "
            "do not invent or shorten IDs. Candidate IDs: " + repr(candidates) +
            (". No backend API is available; omit this REQUEST and describe the missing integration in the handler spec."
             if not api_ids else ". Select only a matching contract; otherwise omit this REQUEST and describe the missing integration in the handler spec."),
            target))
    return errors


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
    create_paths: dict[str, str] = {}
    creates = raw.get("creates", {})
    for table, rows in (creates.items() if isinstance(creates, dict) else []):
        if not isinstance(rows, list):
            continue
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("key"), str):
                continue
            key = row["key"]
            create_paths[key] = f"$.creates.{table}[{i}].key"
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
            if key in create_paths and key != row["key"]:
                errors.append(ShapeError(create_paths[key],
                                         "Compiler-created component root/use symbol; remove this create record, "
                                         "keep references to the compiler symbol", key))
                continue
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
