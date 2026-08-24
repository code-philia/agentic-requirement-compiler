from __future__ import annotations

import inspect
import json
from typing import Any, Awaitable, Callable

from core.service import get_runtime


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


def build_traceability_tools(
    *,
    node_id: str,
    log_cb: LogCallback | None = None,
) -> list[object]:
    """Build read-only traceability DB tools for agents."""

    async def get_requirement_context(req_id: str) -> str:
        """Return a compact requirement neighborhood and its interface/test traceability."""

        requested_req_id = str(req_id or "").strip()
        if not requested_req_id:
            return json.dumps({"error": "req_id is required."}, ensure_ascii=False)
        await _emit_log(
            log_cb,
            "Traceability",
            f"Query compact context for requirement `{requested_req_id}`.",
            node_id=node_id,
        )
        return json.dumps(
            _requirement_context_payload(get_runtime().traceability, requested_req_id),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    async def get_interfaces_for_requirement(req_id: str) -> str:
        """Return raw interface records associated with a requirement id."""

        requested_req_id = str(req_id or "").strip()
        if not requested_req_id:
            return _json_error("req_id is required.")
        await _emit_log(log_cb, "Traceability", f"Query interfaces for requirement `{requested_req_id}`.", node_id=node_id)
        store = get_runtime().traceability
        return _json_records(store.list_interfaces(req_id=requested_req_id))

    async def get_interface(interface_id: str) -> str:
        """Return one raw interface record by interface_id."""

        requested_interface_id = str(interface_id or "").strip()
        if not requested_interface_id:
            return _json_error("interface_id is required.")
        await _emit_log(log_cb, "Traceability", f"Query interface `{requested_interface_id}`.", node_id=node_id)
        record = get_runtime().traceability.get_interface(requested_interface_id)
        if record is None:
            return _json_records([])
        return _json_records([record])

    async def search_interfaces(keyword: str, req_id: str | None = None, interface_type: str | None = None, limit: int = 20) -> str:
        """Search raw interface records by keyword, optional requirement id, and optional interface type."""

        query = str(keyword or "").strip().lower()
        if not query:
            return _json_error("keyword is required.")
        requested_req_id = str(req_id or "").strip()
        requested_type = str(interface_type or "").strip().upper()
        max_items = _normalize_limit(limit)
        await _emit_log(
            log_cb,
            "Traceability",
            f"Search interfaces keyword={query!r} req_id={requested_req_id or '*'} type={requested_type or '*'} limit={max_items}.",
            node_id=node_id,
        )
        store = get_runtime().traceability
        records = store.list_interfaces(req_id=requested_req_id or None)
        matches: list[dict[str, Any]] = []
        for record in records:
            if requested_type and str(record.get("type", "") or "").strip().upper() != requested_type:
                continue
            haystack = json.dumps(record, ensure_ascii=False, default=str).lower()
            if query in haystack:
                matches.append(record)
            if len(matches) >= max_items:
                break
        return _json_records(matches)

    return [get_requirement_context, get_interfaces_for_requirement, get_interface, search_interfaces]


def _requirement_context_payload(store: Any, req_id: str) -> dict[str, Any]:
    current = store.get_requirement(req_id)
    if not isinstance(current, dict):
        return {"error": f"Requirement {req_id!r} was not found."}
    relation_ids: list[tuple[str, str]] = []
    parent = str(current.get("parent_id", "") or "").strip()
    if parent:
        relation_ids.append(("parent", parent))
    relation_ids.extend(
        ("child", str(value).strip())
        for value in current.get("children_ids") or []
        if str(value).strip()
    )
    relation_ids.extend(
        ("dependency", str(value).strip())
        for value in current.get("dependencies") or []
        if str(value).strip()
    )
    relations = []
    for relation, related_id in relation_ids:
        related = store.get_requirement(related_id) or {}
        relations.append(
            {
                "relation": relation,
                "req_id": related_id,
                "name": str(related.get("name", "") or "")[:160],
            }
        )
    interfaces = []
    for item in store.list_interfaces(req_id=req_id)[:20]:
        content = item.get("content", {})
        if not isinstance(content, dict):
            try:
                content = json.loads(str(content or "{}"))
            except json.JSONDecodeError:
                content = {}
        interfaces.append(
            {
                "interface_id": item.get("interface_id", ""),
                "type": item.get("type", ""),
                "file_path": item.get("file_path", ""),
                "implemented": bool(item.get("implemented")),
                "responsibility": str(content.get("responsibility", "") or "")[:220],
            }
        )
    tests = [
        {
            "test_id": item.get("test_id", ""),
            "type": item.get("type", ""),
            "file_path": item.get("file_path", ""),
            "passed": item.get("passed"),
        }
        for item in store.list_tests(req_id=req_id)[:20]
    ]
    return {
        "requirement": {
            "req_id": req_id,
            "name": current.get("name", ""),
            "description": str(current.get("description", "") or "")[:500],
        },
        "relations": relations,
        "interfaces": interfaces,
        "tests": tests,
    }


def _json_records(records: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "count": len(records),
            "interfaces": records,
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )


def _json_error(message: str) -> str:
    return json.dumps({"error": message, "count": 0, "interfaces": []}, ensure_ascii=False, indent=2)


def _normalize_limit(value: Any) -> int:
    try:
        parsed = int(value or 20)
    except (TypeError, ValueError):
        parsed = 20
    return max(1, min(parsed, 100))


async def _emit_log(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    *,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if inspect.isawaitable(result):
        await result
