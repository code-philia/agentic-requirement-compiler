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


def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
    normalized_path = str(path or "").strip()
    if not normalized_path:
        return
    directory = os.path.dirname(normalized_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{normalized_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write("\n")
    os.replace(tmp_path, normalized_path)


def resolve_traceability_db_path(default_path: str) -> str:
    env_path = os.environ.get("ARCBENCH_TRACEABILITY_DB_PATH", "").strip()
    return env_path or default_path


def resolve_runner_events_path() -> str:
    runner_events_path = os.environ.get("ARCBENCH_RUNNER_EVENTS_PATH", "").strip()
    if runner_events_path:
        return runner_events_path
    traceability_events_path = os.environ.get("ARCBENCH_TRACEABILITY_EVENTS_PATH", "").strip()
    return traceability_events_path


def resolve_demo_test_status_path(default_path: str = "/workspace/artifacts/demo-test-statuses.json") -> str:
    env_path = os.environ.get("ARCBENCH_DEMO_TEST_STATUS_PATH", "").strip()
    if env_path:
        return env_path
    runner_events_path = resolve_runner_events_path()
    if runner_events_path:
        return os.path.join(os.path.dirname(runner_events_path), "demo-test-statuses.json")
    traceability_db_path = os.environ.get("ARCBENCH_TRACEABILITY_DB_PATH", "").strip()
    if traceability_db_path:
        return os.path.join(os.path.dirname(traceability_db_path), "demo-test-statuses.json")
    return default_path


def emit_requirement_state(node_id: str, phase: str, status: str, message: str | None = None) -> None:
    normalized_node_id = str(node_id or "").strip()
    runner_events_path = resolve_runner_events_path()
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
    runner_events_path = resolve_runner_events_path()
    if not runner_events_path:
        return
    event_payload = dict(payload)
    event_payload.setdefault("timestamp", _utc_timestamp())
    _append_jsonl(runner_events_path, event_payload)


def emit_refresh_signal(
    *,
    reason: str,
    submission: bool = False,
    logs: bool = False,
    commit_history: bool = False,
    traceability_selected: bool = False,
    traceability_all: bool = False,
    preview: bool = False,
) -> None:
    runner_events_path = resolve_runner_events_path()
    if not runner_events_path:
        return
    _append_jsonl(
        runner_events_path,
        {
            "type": "signal",
            "reason": str(reason or "").strip() or "arc_agent",
            "timestamp": _utc_timestamp(),
            "refresh": {
                "submission": bool(submission),
                "logs": bool(logs),
                "commit_history": bool(commit_history),
                "traceability_selected": bool(traceability_selected),
                "traceability_all": bool(traceability_all),
                "preview": bool(preview),
            },
        },
    )


def _read_demo_test_status_payload() -> dict[str, Any]:
    status_path = resolve_demo_test_status_path()
    if not status_path or not os.path.exists(status_path):
        return {"tests": {}, "requirements": {}}
    try:
        with open(status_path, "r", encoding="utf-8") as source:
            payload = json.load(source)
    except (OSError, json.JSONDecodeError):
        return {"tests": {}, "requirements": {}}
    if not isinstance(payload, dict):
        return {"tests": {}, "requirements": {}}
    tests = payload.get("tests")
    requirements = payload.get("requirements")
    return {
        "tests": tests if isinstance(tests, dict) else {},
        "requirements": requirements if isinstance(requirements, dict) else {},
    }


def _write_demo_test_status_payload(payload: dict[str, Any]) -> None:
    normalized_payload = {
        "tests": payload.get("tests") if isinstance(payload.get("tests"), dict) else {},
        "requirements": payload.get("requirements") if isinstance(payload.get("requirements"), dict) else {},
    }
    _write_json_atomic(resolve_demo_test_status_path(), normalized_payload)


def set_demo_test_status(test_id: str, status: str | None) -> None:
    normalized_test_id = str(test_id or "").strip()
    if not normalized_test_id:
        return
    payload = _read_demo_test_status_payload()
    normalized_status = str(status or "").strip().lower()
    if normalized_status in {"passed", "failed"}:
        payload["tests"][normalized_test_id] = normalized_status
    else:
        payload["tests"].pop(normalized_test_id, None)
    _write_demo_test_status_payload(payload)


def set_demo_test_statuses(status_by_test_id: dict[str, str | None]) -> None:
    if not status_by_test_id:
        return
    payload = _read_demo_test_status_payload()
    for test_id, status in status_by_test_id.items():
        normalized_test_id = str(test_id or "").strip()
        if not normalized_test_id:
            continue
        normalized_status = str(status or "").strip().lower()
        if normalized_status in {"passed", "failed"}:
            payload["tests"][normalized_test_id] = normalized_status
        else:
            payload["tests"].pop(normalized_test_id, None)
    _write_demo_test_status_payload(payload)


def clear_demo_test_statuses(test_ids: list[str]) -> None:
    if not test_ids:
        return
    payload = _read_demo_test_status_payload()
    for test_id in test_ids:
        normalized_test_id = str(test_id or "").strip()
        if normalized_test_id:
            payload["tests"].pop(normalized_test_id, None)
    _write_demo_test_status_payload(payload)


def set_demo_requirement_status(req_id: str, status: str | None) -> None:
    normalized_req_id = str(req_id or "").strip()
    if not normalized_req_id:
        return
    payload = _read_demo_test_status_payload()
    normalized_status = str(status or "").strip().lower()
    if normalized_status in {"passed", "failed"}:
        payload["requirements"][normalized_req_id] = normalized_status
    else:
        payload["requirements"].pop(normalized_req_id, None)
    _write_demo_test_status_payload(payload)
