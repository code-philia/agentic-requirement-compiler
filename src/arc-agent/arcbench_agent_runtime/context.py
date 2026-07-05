from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_RUNNER_EVENTS_PATH = "/workspace/artifacts/runner-events.jsonl"
DEFAULT_TRACEABILITY_DB_PATH = "/workspace/artifacts/traceability.db"
DEFAULT_TRACEABILITY_SNAPSHOT_PATH = "/workspace/artifacts/traceability.snapshot.json"
DEFAULT_DEMO_TEST_STATUS_PATH = "/workspace/artifacts/demo-test-statuses.json"
DEFAULT_PROJECT_DIR = "/workspace/template"


def _resolve_demo_status_path(runner_events_path: Path, traceability_db_path: Path) -> Path:
    env_value = os.environ.get("ARCBENCH_DEMO_TEST_STATUS_PATH", "").strip()
    if env_value:
        return Path(env_value)
    if runner_events_path.parent:
        return runner_events_path.parent / "demo-test-statuses.json"
    return traceability_db_path.parent / "demo-test-statuses.json"


def _resolve_traceability_snapshot_path(traceability_db_path: Path) -> Path:
    env_value = os.environ.get("ARCBENCH_TRACEABILITY_SNAPSHOT_PATH", "").strip()
    if env_value:
        return Path(env_value)
    if traceability_db_path.parent:
        return traceability_db_path.parent / "traceability.snapshot.json"
    return Path(DEFAULT_TRACEABILITY_SNAPSHOT_PATH)


@dataclass(frozen=True)
class RuntimePaths:
    project_dir: Path
    runner_events_path: Path
    traceability_db_path: Path
    traceability_snapshot_path: Path
    demo_test_status_path: Path

    @classmethod
    def from_env(
        cls,
        *,
        project_dir: str | os.PathLike[str] | None = None,
        runner_events_path: str | os.PathLike[str] | None = None,
        traceability_db_path: str | os.PathLike[str] | None = None,
        traceability_snapshot_path: str | os.PathLike[str] | None = None,
        demo_test_status_path: str | os.PathLike[str] | None = None,
    ) -> "RuntimePaths":
        resolved_runner_events = Path(
            runner_events_path
            or os.environ.get("ARCBENCH_RUNNER_EVENTS_PATH", "").strip()
            or os.environ.get("ARCBENCH_TRACEABILITY_EVENTS_PATH", "").strip()
            or DEFAULT_RUNNER_EVENTS_PATH
        )
        resolved_traceability_db = Path(
            traceability_db_path
            or os.environ.get("ARCBENCH_TRACEABILITY_DB_PATH", "").strip()
            or DEFAULT_TRACEABILITY_DB_PATH
        )
        resolved_project_dir = Path(
            project_dir
            or os.environ.get("ARCBENCH_OUTPUT_DIR", "").strip()
            or os.environ.get("ARCBENCH_PROJECT_DIR", "").strip()
            or os.environ.get("ARCBENCH_TEMPLATE_DIR", "").strip()
            or DEFAULT_PROJECT_DIR
        )
        resolved_snapshot = Path(traceability_snapshot_path) if traceability_snapshot_path else _resolve_traceability_snapshot_path(
            resolved_traceability_db,
        )
        resolved_demo_status = Path(demo_test_status_path) if demo_test_status_path else _resolve_demo_status_path(
            resolved_runner_events,
            resolved_traceability_db,
        )
        return cls(
            project_dir=resolved_project_dir,
            runner_events_path=resolved_runner_events,
            traceability_db_path=resolved_traceability_db,
            traceability_snapshot_path=resolved_snapshot,
            demo_test_status_path=resolved_demo_status,
        )

    def ensure_parent_dirs(self) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.runner_events_path.parent.mkdir(parents=True, exist_ok=True)
        self.traceability_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.traceability_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.demo_test_status_path.parent.mkdir(parents=True, exist_ok=True)
