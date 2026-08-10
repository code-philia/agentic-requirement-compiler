from __future__ import annotations

import asyncio
import inspect
import shutil
from typing import Any, Awaitable, Callable


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


async def finalize_subprocess(process: Any, *, force_kill: bool = False) -> None:
    if process is None or getattr(process, "returncode", None) is not None:
        return
    if force_kill:
        process.kill()
    else:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def check_prerequisites(app_type: str, log_cb: LogCallback | None = None) -> bool:
    normalized = (app_type or "web").strip().lower()
    from app_type_handler import get_app_type_handler_class

    required = get_app_type_handler_class(normalized).prerequisite_commands()
    missing = [command for command in required if shutil.which(command) is None]
    if missing:
        await _emit_log(
            log_cb,
            "System",
            f"Missing required command(s) for app_type={normalized}: {', '.join(missing)}",
            status="error",
        )
        return False
    await _emit_log(log_cb, "System", f"Prerequisite check passed for app_type={normalized}.")
    return True


async def _emit_log(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if inspect.isawaitable(result):
        await result
