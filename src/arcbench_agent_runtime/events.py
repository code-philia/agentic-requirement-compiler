from __future__ import annotations

import time
from typing import Any

from .context import RuntimePaths
from .jsonio import append_jsonl, read_json, write_json_atomic


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


class EventClient:
    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths

    def _emit_requirement_state(self, node_id: str, phase: str, status: str, message: str | None = None) -> None:
        normalized_node_id = str(node_id or "").strip()
        if not normalized_node_id:
            return
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "requirement_state",
                "node_id": normalized_node_id,
                "phase": str(phase or "").strip(),
                "status": str(status or "").strip(),
                "timestamp": utc_timestamp(),
                "message": message,
            },
        )

    def mark_design_done(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "design", "completed", message)

    def mark_design_failed(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "design", "failed", message)

    def mark_implementation_done(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "implement", "completed", message)

    def mark_test_passed(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "test", "passed", message)

    def mark_test_failed(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "test", "failed", message)

    def _emit_runner_state(self, state: str, message: str | None = None) -> None:
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "runner_state",
                "state": str(state or "").strip(),
                "timestamp": utc_timestamp(),
                "message": message,
            },
        )

    def mark_run_started(self, message: str | None = None) -> None:
        self._emit_runner_state("running", message)

    def mark_run_completed(self, message: str | None = None) -> None:
        self._emit_runner_state("completed", message)

    def mark_run_failed(self, message: str | None = None) -> None:
        self._emit_runner_state("failed", message)

    def mark_run_paused(self, message: str | None = None) -> None:
        self._emit_runner_state("paused", message)

    def mark_run_resumed(self, message: str | None = None) -> None:
        self._emit_runner_state("resumed", message)

    def _emit_traceability_event(self, payload: dict[str, Any]) -> None:
        normalized = dict(payload)
        normalized.setdefault("timestamp", utc_timestamp())
        append_jsonl(self.paths.runner_events_path, normalized)

    def _emit_refresh_signal(
        self,
        *,
        reason: str,
        submission: bool = False,
        logs: bool = False,
        commit_history: bool = False,
        traceability_selected: bool = False,
        traceability_all: bool = False,
        preview: bool = False,
    ) -> None:
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "signal",
                "reason": str(reason or "").strip() or "arcbench_agent_runtime",
                "timestamp": utc_timestamp(),
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

    def notify_traceability_changed(self, reason: str) -> None:
        self._emit_refresh_signal(
            reason=reason,
            submission=True,
            traceability_selected=True,
            traceability_all=True,
        )

    def notify_commit_history_changed(self, reason: str, *, preview: bool = False) -> None:
        self._emit_refresh_signal(
            reason=reason,
            commit_history=True,
            preview=preview,
        )

    def read_demo_test_status_payload(self) -> dict[str, Any]:
        payload = read_json(self.paths.demo_test_status_path, {"tests": {}, "requirements": {}})
        tests = payload.get("tests")
        requirements = payload.get("requirements")
        return {
            "tests": tests if isinstance(tests, dict) else {},
            "requirements": requirements if isinstance(requirements, dict) else {},
        }

    def write_demo_test_status_payload(self, payload: dict[str, Any]) -> None:
        normalized_payload = {
            "tests": payload.get("tests") if isinstance(payload.get("tests"), dict) else {},
            "requirements": payload.get("requirements") if isinstance(payload.get("requirements"), dict) else {},
        }
        write_json_atomic(self.paths.demo_test_status_path, normalized_payload)

    def set_demo_test_status(self, test_id: str, status: str | None) -> None:
        normalized_test_id = str(test_id or "").strip()
        if not normalized_test_id:
            return
        payload = self.read_demo_test_status_payload()
        normalized_status = str(status or "").strip().lower()
        if normalized_status in {"passed", "failed"}:
            payload["tests"][normalized_test_id] = normalized_status
        else:
            payload["tests"].pop(normalized_test_id, None)
        self.write_demo_test_status_payload(payload)
        self.notify_traceability_changed("demo_test_status_updated")

    def set_demo_test_statuses(self, status_by_test_id: dict[str, str | None]) -> None:
        if not status_by_test_id:
            return
        payload = self.read_demo_test_status_payload()
        for test_id, status in status_by_test_id.items():
            normalized_test_id = str(test_id or "").strip()
            if not normalized_test_id:
                continue
            normalized_status = str(status or "").strip().lower()
            if normalized_status in {"passed", "failed"}:
                payload["tests"][normalized_test_id] = normalized_status
            else:
                payload["tests"].pop(normalized_test_id, None)
        self.write_demo_test_status_payload(payload)
        self.notify_traceability_changed("demo_test_statuses_updated")

    def clear_demo_test_statuses(self, test_ids: list[str]) -> None:
        if not test_ids:
            return
        payload = self.read_demo_test_status_payload()
        for test_id in test_ids:
            normalized_test_id = str(test_id or "").strip()
            if normalized_test_id:
                payload["tests"].pop(normalized_test_id, None)
        self.write_demo_test_status_payload(payload)
        self.notify_traceability_changed("demo_test_statuses_cleared")

    def set_demo_requirement_status(self, req_id: str, status: str | None) -> None:
        normalized_req_id = str(req_id or "").strip()
        if not normalized_req_id:
            return
        payload = self.read_demo_test_status_payload()
        normalized_status = str(status or "").strip().lower()
        if normalized_status in {"passed", "failed"}:
            payload["requirements"][normalized_req_id] = normalized_status
        else:
            payload["requirements"].pop(normalized_req_id, None)
        self.write_demo_test_status_payload(payload)
        self.notify_traceability_changed("demo_requirement_status_updated")
