"""Resolve TDD's edit targets from persisted DESIGN paths, never by searching."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def implementation_scope(workspace_root: str, node_id: str, app_type: str,
                         test_files: list[str], records: list[dict[str, Any]]) -> dict[str, Any]:
    root = Path(workspace_root).resolve()
    from core.sessions import load_node_session
    session = load_node_session(node_id)
    groups = session.get("file_groups")
    if not isinstance(groups, dict):
        # Existing workspaces still have deterministic backend file locations.
        groups = {layer: [record.get("file_path", "") for record in records
                         if record.get("type") == layer
                         and node_id in record.get("req_ids", [])
                         and not str(record.get("interface_id", "")).startswith("GLOBAL:DB:")]
                  for layer in ("API", "FUNC", "DB")}
        groups["shared"] = []
    def paths(values):
        result = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            path = (root / value).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"Implementation target escapes workspace: {value}")
            relative = path.relative_to(root).as_posix()
            if relative.split("/")[0] in {".arc", ".git", "requirements"}:
                raise ValueError(f"Invalid implementation target: {value}")
            result.append(relative)
        return sorted(set(result))
    backend = {layer: paths(groups.get(layer) or []) for layer in ("API", "FUNC", "DB")}
    shared = paths(groups.get("shared") or [])
    for path in [path for values in backend.values() for path in values] + shared:
        if not (root / path).is_file():
            raise ValueError(f"Tracked implementation file is missing: {path}; retry DESIGN to restore its skeleton.")
    frontend_roots = ["frontend"] if app_type == "web" else []
    if app_type == "android":
        frontend_roots = ["app/src/main/res"]
        frontend_roots.extend(path.relative_to(root).as_posix()
                              for path in (root / "app/src/main/java").glob("**/ui") if path.is_dir())
    frontend_files = paths(groups.get("frontend") or [])
    # Backend ownership remains authoritative even if a file was mislabeled shared.
    for path in [path for values in backend.values() for path in values] + shared + frontend_files:
        for record in records:
            if record.get("file_path") == path and record.get("type") != "UI" and (
                str(record.get("interface_id", "")).startswith("GLOBAL:DB:")
                or node_id not in record.get("req_ids", [])
            ):
                raise ValueError(f"File is owned by another requirement/global database: {path}")
    allowed_files = sorted(set([path for values in backend.values() for path in values]
                               + shared + frontend_files + paths(test_files)))
    return {"backend": backend, "frontend_roots": frontend_roots,
            "frontend_files": frontend_files, "shared": shared,
            "allowed_files": allowed_files,
            "rules": "Implement API/FUNC/DB files directly; do not search for backend owners or create replacement modules. Backend writes are limited to these files and listed shared integration files. Locate frontend changes within frontend roots. Tests may be repaired only at listed paths. Other files can be read as direct dependencies but cannot be modified. Missing backend targets require a DESIGN retry."}
