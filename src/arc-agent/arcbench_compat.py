import json
import os
import time
from typing import Any


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _append_jsonl(path: str, payload: dict[str, Any]) -> None:
    normalized_path = str(path or "").strip()
    if not normalized_path:
        return
    directory = os.path.dirname(normalized_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(normalized_path, "a", encoding="utf-8") as output:
        output.write(json.dumps(payload, ensure_ascii=True) + "\n")


def resolve_traceability_db_path(default_path: str) -> str:
    env_path = os.environ.get("ARCBENCH_TRACEABILITY_DB_PATH", "").strip()
    return env_path or default_path


def emit_requirement_state(node_id: str, phase: str, status: str, message: str | None = None) -> None:
    normalized_node_id = str(node_id or "").strip()
    runner_events_path = os.environ.get("ARCBENCH_RUNNER_EVENTS_PATH", "").strip()
    if not normalized_node_id or not runner_events_path:
        return
    _append_jsonl(
        runner_events_path,
        {
            "type": "requirement_state",
            "node_id": normalized_node_id,
            "phase": str(phase or "").strip(),
            "status": str(status or "").strip(),
            "timestamp": _utc_timestamp(),
            "message": message,
        },
    )


def emit_traceability_event(payload: dict[str, Any]) -> None:
    traceability_events_path = os.environ.get("ARCBENCH_TRACEABILITY_EVENTS_PATH", "").strip()
    if not traceability_events_path:
        return
    event_payload = dict(payload)
    event_payload.setdefault("timestamp", _utc_timestamp())
    _append_jsonl(traceability_events_path, event_payload)
