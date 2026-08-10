from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.config import get_workspace_root


def load_node_session(node_id: str) -> dict[str, Any]:
    path = _node_session_path(node_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_node_session(node_id: str, payload: dict[str, Any]) -> None:
    path = _node_session_path(node_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def merge_node_session(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    current = load_node_session(node_id)
    merged = _deep_merge_dict(current, patch)
    save_node_session(node_id, merged)
    return merged


def _node_session_path(node_id: str) -> Path:
    safe_node_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(node_id or "").strip()) or "node"
    return Path(get_workspace_root()) / ".arc" / "node_sessions" / f"{safe_node_id}.json"


def _deep_merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_dict(result[key], value)
        else:
            result[key] = value
    return result
