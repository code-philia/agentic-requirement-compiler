"""JSON response parsing shared by the tool-free compilation stages."""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel


def parse_json_payload(text: str) -> dict[str, Any] | None:
    current = (text or "").strip()
    for _ in range(3):
        if not current:
            return None
        next_string: str | None = None
        for candidate in _json_candidates(current):
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            normalized = _normalize_payload_value(payload)
            if normalized is not None:
                return normalized
            if isinstance(payload, str) and payload.strip():
                next_string = payload.strip()
        if next_string is None:
            return None
        current = next_string
    return None


def _normalize_payload_value(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump()
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {"items": dumped}
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value}
    return None


def _json_candidates(text: str) -> list[str]:
    candidates: list[str] = []

    def add(candidate: str) -> None:
        stripped = candidate.strip()
        if stripped and stripped not in candidates:
            candidates.append(stripped)

    add(text)
    for match in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE):
        add(match)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end != -1 and end > start:
            add(text[start : end + 1])
    return candidates
