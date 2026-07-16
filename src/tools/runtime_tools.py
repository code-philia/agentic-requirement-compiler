from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
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

        workspace_path = Path(getattr(app_handler, "workspace_path", ".")).expanduser().resolve()
        app_type = str(getattr(app_handler, "name", "") or "web").strip().lower()
        await _emit_log(log_cb, "Compiler", "System is executing build verification.", node_id=node_id)
        if app_type == "android":
            return await _run_android_build(workspace_path)
        return await _run_web_build(workspace_path)

    return run_build


async def _run_web_build(workspace_path: Path) -> str:
    frontend_result = await _run_fixed_command(
        "npm run build",
        cwd=workspace_path / "frontend",
        timeout=120.0,
        env=_build_base_env(),
    )
    backend_result = await _run_fixed_command(
        "npm run build --if-present",
        cwd=workspace_path / "backend",
        timeout=120.0,
        env=_build_base_env(),
    )
    return f"=== Frontend Build Result ===\n{frontend_result}\n\n=== Backend Build Result ===\n{backend_result}"


async def _run_android_build(workspace_path: Path) -> str:
    command = "cmd /c gradlew.bat assembleDebug compileDebugUnitTestJavaWithJavac --info" if os.name == "nt" else "./gradlew assembleDebug compileDebugUnitTestJavaWithJavac --info"
    result = await _run_fixed_command(
        command,
        cwd=workspace_path,
        timeout=180.0,
        env=_build_base_env(),
        max_output_chars=30000,
    )
    return f"=== Android Build Result ===\n{result}"


async def _run_fixed_command(
    command: str,
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str],
    max_output_chars: int = 4000,
) -> str:
    if not cwd.exists():
        return f"Exit Code: 1\nSTDERR:\nWorking directory does not exist: {cwd}\n"

    process = None
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        result = f"Exit Code: {process.returncode}\n"
        if output:
            result += f"STDOUT:\n{output}\n"
        if error:
            result += f"STDERR:\n{error}\n"
        return _truncate_middle(result, max_output_chars)
    except asyncio.TimeoutError:
        if process is not None:
            await _terminate_process(process)
        return f"Exit Code: 124\nSTDERR:\nCommand timed out after {timeout} seconds.\n"
    except Exception as exc:
        return f"Exit Code: 1\nSTDERR:\nExecution failed: {str(exc)}\n"


def _build_base_env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8",
    }


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


def _truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = max(1, (limit - 32) // 2)
    return text[:half] + "\n...[OUTPUT TRUNCATED]...\n" + text[-half:]


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
