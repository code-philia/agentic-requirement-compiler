from __future__ import annotations

from pathlib import Path
from typing import Any

from arcbench_agent_runtime.runtime import AgentRuntime
from context.context_pipeline import set_context_config, set_context_runtime


_runtime: AgentRuntime | None = None


def configure_runtime(
    *,
    project_dir: str,
    traceability_dir: str | None = None,
    runner_events_path: str | None = None,
    app_type: str | None = None,
    web_port: int | None = None,
    android_package: str | None = None,
) -> AgentRuntime:
    """Create and publish the process-wide ARC runtime."""

    global _runtime
    resolved_project_dir = str(Path(project_dir).expanduser().resolve())
    _runtime = AgentRuntime.from_env(
        project_dir=resolved_project_dir,
        runner_events_path=runner_events_path,
        traceability_dir=traceability_dir,
    )
    set_context_runtime(_runtime)
    set_context_config(
        workspace_dir=resolved_project_dir,
        app_type=app_type,
        web_port=web_port,
        android_package=android_package,
    )
    return _runtime


def get_runtime() -> AgentRuntime:
    if _runtime is None:
        raise RuntimeError("ARC runtime has not been configured.")
    return _runtime


def has_runtime() -> bool:
    return _runtime is not None


def reset_runtime_for_tests() -> None:
    global _runtime
    _runtime = None
    set_context_runtime(None)
