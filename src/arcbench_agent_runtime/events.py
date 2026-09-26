from __future__ import annotations

import time

from .context import RuntimePaths
from .jsonio import append_jsonl


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


class EventClient:
    def __init__(self, paths: RuntimePaths, *, run_id: str | None = None) -> None:
        self.paths = paths
        self.run_id = str(run_id or "").strip() or None
        self.phase = "INITIALIZING"
        self.entered_tdd = False

    def _emit_runner_state(self, state: str, message: str) -> None:
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "runner_state",
                "run_id": self.run_id,
                "phase": self.phase,
                "state": state,
                "fatal": state == "failed",
                "recoverable": state != "failed",
                "entered_tdd": self.entered_tdd,
                "timestamp": utc_timestamp(),
                "message": message,
            },
        )

    def mark_run_started(self, message: str, *, phase: str = "PREPROCESSING") -> None:
        self.phase = str(phase or "PREPROCESSING").strip().upper()
        self._emit_runner_state("running", message)

    def mark_phase_started(self, phase: str, message: str) -> None:
        self.phase = str(phase).strip().upper() or self.phase
        if self.phase == "TDD":
            self.entered_tdd = True
        self._emit_runner_state("running", message)

    def mark_run_completed(self, message: str) -> None:
        self._emit_runner_state("completed", message)

    def mark_run_failed(self, message: str) -> None:
        self._emit_runner_state("failed", message)

    def notify_traceability_changed(self, reason: str) -> None:
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "signal",
                "run_id": self.run_id,
                "phase": self.phase,
                "fatal": False,
                "recoverable": True,
                "entered_tdd": self.entered_tdd,
                "reason": reason,
                "timestamp": utc_timestamp(),
                "refresh": {
                    "submission": True,
                    "logs": False,
                    "commit_history": False,
                    "traceability_selected": True,
                    "traceability_all": True,
                    "preview": False,
                },
            },
        )
