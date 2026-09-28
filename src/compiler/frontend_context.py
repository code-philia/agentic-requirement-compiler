"""Read-only context selection for consumers of the seven-entity frontend IR."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


def frontend_subgraph(ir: dict[str, Any], requirement_id: str, target_ids: set[str]) -> dict[str, Any]:
    tables = ("components", "ui", "properties", "events", "handlers", "effects")
    selected = set(target_ids)
    for table in (*tables, "data"):
        for row in ir.get(table, []):
            if requirement_id in row.get("requirement_ids", []):
                selected.add(row.get("component_id") or row.get("owner_id") or row["id"])
    # Include descendants, then ancestors; do not pull unrelated sibling pages in through App.
    changed = True
    while changed:
        before = set(selected)
        for row in ir.get("ui", []):
            if row.get("kind") == "COMPONENT" and row.get("component_id") in selected:
                selected.add(row["component_ref"])
        changed = selected != before
    changed = True
    while changed:
        before = set(selected)
        for row in ir.get("ui", []):
            if row.get("kind") == "COMPONENT" and row.get("component_ref") in selected:
                selected.add(row["component_id"])
        changed = selected != before
    # Keep both component-owned records and records directly attributed to the
    # requirement.  The latter matters for reused UI/behavior nodes whose
    # owning component was discovered through a separate composition edge.
    result = {
        table: [
            copy.deepcopy(row)
            for row in ir.get(table, [])
            if row.get("component_id", row["id"]) in selected
            or requirement_id in row.get("requirement_ids", [])
        ]
        for table in tables
    }
    owners = {row["id"] for rows in result.values() for row in rows}
    result["data"] = [
        copy.deepcopy(row) for row in ir.get("data", [])
        if row.get("owner_id") in owners or requirement_id in row.get("requirement_ids", [])
    ]
    result["root_component_id"] = ir.get("root_component_id")
    result["entry_routes"] = ["/"] if result["components"] else []
    return result


def visual_context(project_root: Path, requirement_id: str) -> list[dict[str, Any]]:
    root = project_root / ".arc/design/frontend"
    cache_path = root / "visual-references.json"
    if not cache_path.is_file():
        return []
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(cached, dict):
        return []

    # Traceability is the authoritative requirement -> visual link.  Older
    # caches may only use the image path as their key, so retain both ids and
    # paths while matching records.
    linked_ids: set[str] = set()
    linked_paths: set[str] = set()
    frontend: dict[str, Any] = {}
    frontend_path = root / "frontend.json"
    if frontend_path.is_file():
        try:
            loaded = json.loads(frontend_path.read_text(encoding="utf-8"))
            frontend = loaded if isinstance(loaded, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            frontend = {}
    for table_rows in frontend.values():
        if not isinstance(table_rows, list):
            continue
        for row in table_rows:
            if not isinstance(row, dict) or requirement_id not in {
                str(value) for value in row.get("requirement_ids", [])
            }:
                continue
            linked_ids.update(str(value) for value in row.get("visual_reference_ids", []) if value)
            for key in ("visual_reference_path", "source_path"):
                if row.get(key):
                    linked_paths.add(str(row[key]).replace(chr(92), "/"))
    trace_path = root / "traceability.json"
    if trace_path.is_file():
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            trace = {}
        links = trace.get("requirements", {}).get(requirement_id, {}) if isinstance(trace, dict) else {}
        if isinstance(links, dict):
            for key in ("visual_reference_ids", "visual_references"):
                values = links.get(key, [])
                if isinstance(values, list):
                    linked_ids.update(str(value) for value in values if value)
            for key in ("visual_reference_paths", "visual_paths"):
                values = links.get(key, [])
                if isinstance(values, list):
                    linked_paths.update(str(value).replace(chr(92), "/") for value in values if value)
    # Batch records retain the resolved reference id even when the final
    # traceability index intentionally omits image payloads.
    batch_root = root / "batches"
    if batch_root.is_dir():
        for batch_path in batch_root.glob("*.json"):
            try:
                batch = json.loads(batch_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(batch, dict) or requirement_id not in {
                str(value) for value in batch.get("requirement_ids", [])
            }:
                continue
            stack: list[Any] = [batch]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    for key, nested in value.items():
                        if key in {"visual_reference_id", "reference_id"} and nested:
                            linked_ids.add(str(nested))
                        elif key in {"visual_reference_path", "source_path"} and nested:
                            linked_paths.add(str(nested).replace(chr(92), "/"))
                        stack.append(nested)
                elif isinstance(value, list):
                    stack.extend(value)

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cache_key, raw in cached.items():
        if not isinstance(raw, dict):
            continue
        record = copy.deepcopy(raw)
        record_id = str(record.get("id", record.get("reference_id", cache_key)))
        source_path = str(record.get("source_path", record.get("path", cache_key))).replace(chr(92), "/")
        owns_requirement = requirement_id in {
            str(value) for value in record.get("requirement_ids", [])
        }
        linked = record_id in linked_ids or source_path in linked_paths
        # Some runs persist requirement links only in frontend.json.
        if frontend and not (owns_requirement or linked):
            for row in frontend.get("visual_references", []) if isinstance(frontend, dict) else []:
                if not isinstance(row, dict):
                    continue
                row_id = str(row.get("id", row.get("reference_id", "")))
                if requirement_id in {str(value) for value in row.get("requirement_ids", [])} and (
                    row_id == record_id or str(row.get("source_path", "")).replace(chr(92), "/") == source_path
                ):
                    linked = True
        if not (owns_requirement or linked):
            continue
        if record_id in seen:
            continue
        record.setdefault("id", record_id)
        records.append(record)
        seen.add(record_id)
    return records
