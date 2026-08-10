from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml


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
    return payload


def read_json_file(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    candidate = Path(path)
    if not candidate.exists():
        return None
    try:
        with candidate.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json_file(path: str | os.PathLike[str], payload: dict[str, Any]) -> None:
    candidate = Path(path)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = candidate.with_suffix(candidate.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")
    tmp_path.replace(candidate)
