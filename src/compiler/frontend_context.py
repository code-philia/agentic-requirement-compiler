"""Read-only context selection for consumers of the seven-entity frontend IR."""
from __future__ import annotations

import copy
import json
import posixpath
from pathlib import Path
from typing import Any


def implementation_requirement(
    requirement: dict[str, Any], project_root: Path, *, frontend: bool,
) -> tuple[dict[str, Any], list[str]]:
    """Project visual references for model input without mutating requirement IR."""
    cache: dict[str, Any] = {}
    if frontend:
        path = project_root / ".arc/design/frontend/visual_cache.json"
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = {posixpath.normpath(key.replace(chr(92), "/")): row
                         for key, row in loaded.items()}
    missing: list[str] = []

    def project(value: Any) -> Any:
        if isinstance(value, list):
            return [project(item) for item in value]
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        result = {}
        for key, item in value.items():
            if key != "visual_references":
                result[key] = project(item)
                continue
            if not frontend:
                continue
            analyses = []
            seen: set[str] = set()
            for reference in item:
                normalized = posixpath.normpath(reference.replace(chr(92), "/"))
                if normalized in seen:
                    continue
                seen.add(normalized)
                record = cache.get(normalized)
                if isinstance(record, dict) and isinstance(record.get("analysis"), dict):
                    analyses.append(copy.deepcopy(record["analysis"]))
                else:
                    missing.append(reference)
            result[key] = analyses
        return result

    return project(requirement), sorted(set(missing))


def component_requirement_owners(ir: dict[str, Any]) -> dict[str, set[str]]:
    """Resolve explicit entity ownership without spreading across composition edges."""
    tables = ("components", "ui", "data", "properties", "events", "handlers", "effects")
    rows = {row["id"]: row for table in tables for row in ir.get(table, [])}
    owners = {row["id"]: set() for row in ir.get("components", [])}
    for row in rows.values():
        current = row
        visited: set[str] = set()
        while current["id"] not in owners and current["id"] not in visited:
            visited.add(current["id"])
            parent = current.get("component_id") or current.get("owner_id")
            if parent not in rows:
                break
            current = rows[parent]
        if current["id"] in owners:
            owners[current["id"]].update(row.get("requirement_ids", []))
        # An explicitly associated component use also associates its target,
        # but does not propagate the containing component's other owners.
        if row.get("kind") == "COMPONENT" and row.get("component_ref") in owners:
            owners[row["component_ref"]].update(row.get("requirement_ids", []))
    return owners


def frontend_subgraph(ir: dict[str, Any], requirement_id: str, target_ids: set[str]) -> dict[str, Any]:
    tables = ("components", "ui", "properties", "events", "handlers", "effects")
    ownership = component_requirement_owners(ir)
    selected = (set(target_ids) & ownership.keys()) or {
        cid for cid, requirements in ownership.items() if requirement_id in requirements
    }
    selected |= {
        row["component_ref"] for row in ir.get("ui", [])
        if row.get("kind") == "COMPONENT" and row.get("component_id") in selected
    }
    # One composition hop supplies child contracts without pulling ancestors
    # and their unrelated sibling pages into the context.
    result = {
        table: [
            copy.deepcopy(row)
            for row in ir.get(table, [])
            if row.get("component_id", row["id"]) in selected
        ]
        for table in tables
    }
    owners = {row["id"] for rows in result.values() for row in rows}
    result["data"] = [
        copy.deepcopy(row) for row in ir.get("data", [])
        if row.get("owner_id") in owners
    ]
    result["root_component_id"] = ir.get("root_component_id")
    result["entry_routes"] = ["/"] if result["components"] else []
    return result


def visual_context(project_root: Path, requirement_id: str) -> list[dict[str, Any]]:
    root = project_root / ".arc/design/frontend"
    # The exported reference table may be empty while the path-keyed cache
    # contains the actual analyses. Read both; an index must not mask evidence.
    cached: dict[str, Any] = {}
    for filename in ("visual_cache.json", "visual-references.json", "visual_references.json"):
        path = root / filename
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        rows = payload.get("visual_references")
        entries = enumerate(rows) if isinstance(rows, list) else payload.items()
        for key, row in entries:
            if not isinstance(row, dict):
                continue
            identity = str(row.get("id") or row.get("reference_id") or key)
            previous = cached.get(identity, {})
            merged = {**previous, **row}
            if previous.get("analysis") and not row.get("analysis"):
                merged["analysis"] = previous["analysis"]
            merged["requirement_ids"] = sorted(set(previous.get("requirement_ids", []))
                                               | set(row.get("requirement_ids", [])))
            merged.setdefault("source_path", str(key))
            cached[identity] = merged

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
    trace_path = root / "requirements.json"
    if not trace_path.is_file():
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
