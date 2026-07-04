from __future__ import annotations

from arcbench_agent_runtime import AgentRuntime


_RUNTIME: AgentRuntime | None = None
_RUNTIME_KWARGS: dict[str, str] = {}


def configure_runtime(
    *,
    project_dir: str | None = None,
    runner_events_path: str | None = None,
    traceability_db_path: str | None = None,
    demo_test_status_path: str | None = None,
) -> AgentRuntime:
    global _RUNTIME

    current = dict(_RUNTIME_KWARGS)
    if project_dir is not None:
        current["project_dir"] = str(project_dir)
    if runner_events_path is not None:
        current["runner_events_path"] = str(runner_events_path)
    if traceability_db_path is not None:
        current["traceability_db_path"] = str(traceability_db_path)
    if demo_test_status_path is not None:
        current["demo_test_status_path"] = str(demo_test_status_path)

    _RUNTIME_KWARGS.clear()
    _RUNTIME_KWARGS.update(current)
    _RUNTIME = AgentRuntime.from_env(**_RUNTIME_KWARGS)
    return _RUNTIME


def get_runtime() -> AgentRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = AgentRuntime.from_env(**_RUNTIME_KWARGS)
    return _RUNTIME
