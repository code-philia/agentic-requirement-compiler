"""File selection shared by model consumers; reasons stay in compiler logs."""
from __future__ import annotations

from typing import Any


def direct_dependencies(
    targets: list[dict[str, Any]], candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    owned = {row["module_id"] for row in targets}
    referenced = {key for row in targets for key in row.get("callees", [])} - owned
    return [row for row in candidates if row["module_id"] in referenced]


def file_selection_log(requirement_id: str, phase: str, files: dict[str, str], reason: str) -> str:
    return (f"FILE_CONTEXT requirement={requirement_id} phase={phase} reason={reason} "
            f"files={sorted(files)} chars={sum(len(source) for source in files.values())}")
