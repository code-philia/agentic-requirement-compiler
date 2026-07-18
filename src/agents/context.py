from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRuntimeContext:
    """Per-run metadata for ARC stage agents.

    This context is for tools, logging, and future middleware. Model-visible
    requirement context should still be assembled into the user message by the
    context pipeline.
    """

    node_id: str
    phase: str
    app_type: str
    workspace_root: str
    requirement_path: str
    test_type: str = ""

