from __future__ import annotations

import inspect
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from arcbench_agent_runtime.runtime import AgentRuntime
from compiler import CompilationRequest, Compiler
from core.config import set_workspace_root
from core.logging import set_run_id


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


class ARCWorkflowManager:
    """Configure one compiler run and expose its result to the CLI."""

    def __init__(
        self,
        workspace_path: str,
        requirement_path: str = "",
        web_port: int = 3000,
        log_cb: LogCallback | None = None,
    ) -> None:
        self.workspace_path = Path(workspace_path).expanduser().resolve()
        self.requirement_path = Path(requirement_path).expanduser().resolve()
        self.web_port = int(web_port)
        self.log_cb = log_cb or _default_log_cb

    async def start_compilation(self) -> dict[str, object]:
        run_id = uuid.uuid4().hex
        set_run_id(run_id)
        await self._log("Compiler", "ARC compilation started.")
        self.workspace_path.mkdir(parents=True, exist_ok=True)
        set_workspace_root(self.workspace_path)
        runtime = AgentRuntime.for_project(self.workspace_path, run_id=run_id)
        runtime.traceability.init_store()
        runtime.events.mark_run_started(
            "ARC deterministic compiler run started.",
            phase="PROJECT",
        )

        compiler = Compiler(runtime, self.log_cb)
        result = await compiler.compile(
            CompilationRequest(
                requirement_path=self.requirement_path,
                output_dir=self.workspace_path,
                web_port=self.web_port,
            )
        )
        if result.ok:
            if result.failed_nodes:
                runtime.events.mark_run_completed(
                    "ARC compilation completed with skipped requirements."
                )
                await self._log(
                    "Compiler",
                    "Compilation finished with skipped requirements: "
                    f"{sorted(result.failed_nodes)}.",
                    "warning",
                )
            else:
                runtime.events.mark_run_completed("ARC compilation completed.")
                await self._log("Compiler", "Compilation finished successfully.")
        else:
            runtime.events.mark_run_failed("ARC compiler pass failed.")
            await self._log("Compiler", "Compilation failed; inspect the compiler log.", "error")
        payload = result.to_dict()
        payload["run_id"] = run_id
        return payload

    async def _log(
        self,
        agent_name: str,
        message: str,
        status: str | None = None,
        node_id: str | None = None,
    ) -> None:
        result = self.log_cb(agent_name, message, status, node_id)
        if inspect.isawaitable(result):
            await result


async def _default_log_cb(
    agent_name: str,
    message: str,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    from core.logging import append_debug_log, write_terminal_log

    append_debug_log(agent_name, message, status=status, node_id=node_id)
    write_terminal_log(agent_name, message, status=status, node_id=node_id)
