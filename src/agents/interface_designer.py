from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from agents.context import AgentRuntimeContext
from agents.factory import build_stage_agent
from agents.runners import ainvoke_stage_agent
from context.context_pipeline import context_pipeline
from context.prompts.interface_designer import get_system_prompt, get_user_prompt
from tools.traceability_tools import build_traceability_tools


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


class InterfaceDesignResponse(BaseModel):
    summary: str = Field(default="", description="Short design-stage summary.")
    interfaces: list[dict[str, Any]] = Field(default_factory=list, description="Interface contracts for the current node.")
    files_written: list[str] = Field(default_factory=list, description="Workspace-relative files written or edited.")


class InterfaceDesigner:
    """Deep-agents based interface-design stage adapter."""

    agent_name = "InterfaceDesigner"

    def __init__(
        self,
        log_cb: LogCallback | None = None,
        *,
        model: str | object | None = None,
        workspace_root: str | None = None,
        requirement_path: str | None = None,
        app_type: str | None = None,
    ) -> None:
        self.log_cb = log_cb
        self.model = model or os.environ.get("MODEL", "openai:gpt-5.4")
        self.workspace_root = workspace_root
        self.requirement_path = requirement_path or ""
        self.app_type = app_type

    async def run(
        self,
        *,
        node_id: str,
        requirement_data: dict[str, Any],
    ) -> dict[str, Any]:
        workspace_root = str(Path(
            self.workspace_root
            or context_pipeline.config.workspace_dir
            or os.environ.get("ARC_WORKSPACE_ROOT")
            or os.getcwd()
        ).expanduser().resolve())
        app_type = (self.app_type or context_pipeline.config.app_type or os.environ.get("ARC_APP_TYPE") or "web").strip().lower()
        skill_root = Path(__file__).resolve().parents[1] / "skills"
        is_non_leaf = bool(requirement_data.get("children_ids"))
        skill_names = ["non-leaf-ui-only-design"] if is_non_leaf else ["leaf-full-design"]
        skill_names.append("auth-session-consistency")
        context_pipeline.configure(
            workspace_dir=workspace_root,
            app_type=app_type,
        )
        static_context, dynamic_context = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
        )
        context_text = "\n\n".join(part.strip() for part in (static_context, dynamic_context) if part.strip())

        agent = build_stage_agent(
            name="interface_designer",
            model=self.model,
            system_prompt=get_system_prompt(),
            response_format=InterfaceDesignResponse,
            workspace_root=workspace_root,
            writable_roots=[workspace_root],
            skills=[f"/skills/{name}/" for name in skill_names if (skill_root / name / "SKILL.md").exists()],
            memory=[],
            tools=build_traceability_tools(node_id=node_id, log_cb=self.log_cb),
        )
        message = get_user_prompt(
            node_id=node_id,
            requirement_data=requirement_data,
            dynamic_context=context_text,
        )
        await self._log("Invoking deep-agent interface design.", node_id=node_id)
        payload = await ainvoke_stage_agent(
            agent,
            message=message,
            context=AgentRuntimeContext(
                node_id=node_id,
                phase="DESIGN",
                app_type=app_type,
                workspace_root=workspace_root,
                requirement_path=self.requirement_path,
            ),
            thread_id=f"{node_id}:DESIGN:InterfaceDesigner",
            label=self.agent_name,
            log_cb=self.log_cb,
        )
        bundle = self._normalize_design_payload(payload)
        await self._log(
            f"Interface design returned {len(bundle.get('interfaces', []))} interface(s).",
            node_id=node_id,
        )
        return bundle

    def _normalize_design_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        interfaces = payload.get("interfaces")
        if interfaces is None and isinstance(payload.get("items"), list):
            interfaces = payload["items"]
        if not isinstance(interfaces, list):
            interfaces = []
        normalized_interfaces = [item for item in interfaces if isinstance(item, dict)]
        files_written = payload.get("files_written") or payload.get("files") or []
        if not isinstance(files_written, list):
            files_written = []
        return {
            "summary": str(payload.get("summary", "") or "").strip(),
            "interfaces": normalized_interfaces,
            "files_written": [str(path).strip() for path in files_written if str(path).strip()],
        }

    async def _log(self, message: str, status: str | None = None, node_id: str | None = None) -> None:
        if self.log_cb is None:
            return
        result = self.log_cb(self.agent_name, message, status, node_id)
        if inspect.isawaitable(result):
            await result
