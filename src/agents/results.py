from __future__ import annotations

from typing import Any


def normalize_test_manifest_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize structured or fallback agent output into test manifest items."""

    candidates = payload.get("tests")
    if candidates is None:
        candidates = payload.get("items")
    if candidates is None and _looks_like_test_item(payload):
        candidates = [payload]
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if isinstance(item, dict) and _looks_like_test_item(item)]


def _looks_like_test_item(value: dict[str, Any]) -> bool:
    return bool(
        str(value.get("test_id", "")).strip()
        or str(value.get("file_path", "")).strip()
        or str(value.get("type", "")).strip()
    )
