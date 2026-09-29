"""File selection shared by model consumers; reasons stay in compiler logs."""
from __future__ import annotations

from typing import Any


def direct_dependencies(
    targets: list[dict[str, Any]], candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    owned = {row["module_id"] for row in targets}
    referenced = {key for row in targets for key in row.get("callees", [])} - owned
    return [row for row in candidates if row["module_id"] in referenced]


def requirement_dependencies(
    targets: list[dict[str, Any]], candidates: list[dict[str, Any]],
    requirement_id: str, frontend_ir: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Scope component callees to the requirement's UI/behavior, not all of App."""
    if frontend_ir is None or "root_component_id" not in frontend_ir:
        return direct_dependencies(targets, candidates)
    from .frontend_workspace import references

    component_ids = {row["id"] for row in frontend_ir.get("components", [])}
    target_ids = {row["module_id"] for row in targets}
    rows = {row["id"]: row for table in ("ui", "data", "properties", "events", "handlers", "effects")
            for row in frontend_ir.get(table, [])}
    pending = [key for key, row in rows.items()
               if requirement_id in row.get("requirement_ids", [])]
    selected: set[str] = set()
    module_ids = {key for row in targets if row["module_id"] not in component_ids
                  for key in row.get("callees", [])}
    events_by_ui: dict[str, list[str]] = {}
    for event in frontend_ir.get("events", []):
        if event.get("ui_id"):
            events_by_ui.setdefault(event["ui_id"], []).append(event["id"])
    while pending:
        key = pending.pop()
        if key in selected or key not in rows:
            continue
        selected.add(key)
        row = rows[key]
        if row.get("component_ref"):
            module_ids.add(row["component_ref"])
        if row.get("kind") == "REQUEST" and row.get("target"):
            module_ids.add(row["target"])
        pending.extend(events_by_ui.get(key, []))
        for field, reference in references(row):
            if field in {"component_id", "component_ref", "ui_root_id"} or reference in component_ids:
                continue
            # A shared container's children are not all part of this task.
            if field == "children" and reference in rows:
                owners = rows[reference].get("requirement_ids", [])
                if owners and requirement_id not in owners:
                    continue
            pending.append(reference)
    return [row for row in candidates if row["module_id"] in module_ids - target_ids]


def file_selection_log(requirement_id: str, phase: str, files: dict[str, str], reason: str) -> str:
    return (f"FILE_CONTEXT requirement={requirement_id} phase={phase} reason={reason} "
            f"files={sorted(files)} chars={sum(len(source) for source in files.values())}")
