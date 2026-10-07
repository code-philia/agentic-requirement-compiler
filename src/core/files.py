from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from arcbench_agent_runtime.jsonio import read_json, write_json_atomic


def normalize_windows_extended_prefix_text(value: str | Path | None) -> str:
    """Normalize separators and Windows extended prefixes for path comparisons."""
    text = str(value or "").strip().replace("\\", "/")
    if text.startswith("//?/UNC/"):
        return "//" + text[len("//?/UNC/"):]
    if text.startswith("//?/"):
        return text[len("//?/"):]
    return text


def load_requirements(requirement_path: str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(requirement_path)
    with path.open("r", encoding="utf-8") as file:
        payload = yaml.safe_load(file) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Requirement file must contain a mapping: {path}")
    if isinstance(payload.get("root"), dict):
        payload = payload["root"]
    if "id" not in payload and isinstance(payload.get("requirement"), dict):
        payload = payload["requirement"]
    if not str(payload.get("id", "")).strip():
        raise ValueError(f"Requirement root node id is missing: {path}")
    from arcbench_agent_runtime.requirement_contracts import resolve_requirement_contracts
    return resolve_requirement_contracts(payload)


def read_json_file(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    return read_json(Path(path))


def write_json_file(path: str | os.PathLike[str], payload: dict[str, Any]) -> None:
    write_json_atomic(Path(path), payload)
