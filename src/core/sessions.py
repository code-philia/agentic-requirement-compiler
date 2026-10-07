from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from arcbench_agent_runtime.jsonio import read_json, write_json_atomic


def load_node_session(node_id: str, *, workspace_dir: str | Path | None = None) -> dict[str, Any]:
    return read_json(_node_session_path(node_id, workspace_dir=workspace_dir), {}) or {}


def save_node_session(node_id: str, payload: dict[str, Any]) -> None:
    write_json_atomic(_node_session_path(node_id), payload)


def merge_node_session(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    current = load_node_session(node_id)
    merged = _deep_merge_dict(current, patch)
    save_node_session(node_id, merged)
    return merged


def _node_session_path(node_id: str, *, workspace_dir: str | Path | None = None) -> Path:
    safe_node_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(node_id or "").strip()) or "node"
    if workspace_dir is None:
        # ContextPipeline supplies its own root; only legacy callers use process config.
        from core.config import get_workspace_root
        workspace_dir = get_workspace_root()
    root = Path(workspace_dir)
    return root / ".arc" / "node_sessions" / f"{safe_node_id}.json"


def _deep_merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_dict(result[key], value)
        else:
            result[key] = value
    return result
