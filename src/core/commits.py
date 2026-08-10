from __future__ import annotations

from typing import Any


def build_commit_message(node_id: str, phase: str, requirement_data: dict[str, Any]) -> str:
    name = str(requirement_data.get("name") or node_id).strip()
    normalized_phase = str(phase or "").strip().lower()
    return f"{node_id} ({normalized_phase}): {name}"
