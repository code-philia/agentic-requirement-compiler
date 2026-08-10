from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


def build_run_build_tool(
    *,
    app_handler: Any,
    node_id: str,
    log_cb: LogCallback | None = None,
):
    """Build the system-owned run_build tool for the current workspace."""

    async def run_build() -> str:
        """Run the system-defined build verification. Takes no arguments."""

        await _emit_log(log_cb, "Compiler", "System is executing build verification.", node_id=node_id)
        if not hasattr(app_handler, "run_build"):
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                "System build runner is not configured for this app handler.\n"
            )
        return await app_handler.run_build()

    return run_build


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
