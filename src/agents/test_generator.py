from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from agents.context import AgentRuntimeContext
from agents.factory import build_stage_agent
from agents.runners import ainvoke_stage_agent
from context.context_pipeline import context_pipeline
from context.prompts.test_generator import get_system_prompt, get_user_prompt
from tools.result_parsers import normalize_test_manifest_payload
from tools.traceability_tools import build_traceability_tools


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


class TestManifestItem(BaseModel):
    test_id: str = Field(description="Stable test artifact id.")
    req_id: str = Field(description="Requirement node id covered by this test.")
    interface_ids: list[str] = Field(default_factory=list, description="Covered interface ids.")
    type: str = Field(description="Unit, Integration, or E2E.")
    file_path: str = Field(description="Workspace-relative test file path.")
    first_line: str = Field(default="", description="Exact first line in the written test file.")


class TestGenerationResponse(BaseModel):
    summary: str = Field(default="", description="Short test-design summary.")
    tests: list[TestManifestItem] = Field(default_factory=list, description="Generated test manifest.")
    files_written: list[str] = Field(default_factory=list, description="Workspace-relative files written or edited.")


class TestGenerator:
    """Deep-agents based test-generation stage adapter."""

    agent_name = "TestGenerator"

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
        node_id: str,
        requirement_data: dict[str, Any],
        *,
        preloaded_source: str | None = None,
    ) -> tuple[list[dict[str, Any]] | None, str]:
        workspace_root = str(Path(
            self.workspace_root
            or context_pipeline.config.workspace_dir
            or os.environ.get("ARC_WORKSPACE_ROOT")
            or os.getcwd()
        ).expanduser().resolve())
        app_type = (self.app_type or context_pipeline.config.app_type or os.environ.get("ARC_APP_TYPE") or "web").strip().lower()
        skill_root = Path(__file__).resolve().parents[1] / "skills"
        skill_names = ["leaf-test-layer-selection", "auth-session-consistency"]
        context_pipeline.configure(
            workspace_dir=workspace_root,
            app_type=app_type,
        )
        static_context, dynamic_context = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
        )
        interface_contract = context_pipeline.get_interface_contract_context(node_id)
        context_text = "\n\n".join(part.strip() for part in (static_context, dynamic_context) if part.strip())
        agent = build_stage_agent(
            name="test_generator",
            model=self.model,
            system_prompt=get_system_prompt(),
            response_format=TestGenerationResponse,
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
            interface_contract=interface_contract,
        )
        await self._log("Invoking deep-agent test generation.", node_id=node_id)
        raw_payload = await ainvoke_stage_agent(
            agent,
            message=message,
            context=AgentRuntimeContext(
                node_id=node_id,
                phase="DESIGN",
                app_type=app_type,
                workspace_root=workspace_root,
                requirement_path=self.requirement_path,
            ),
            thread_id=f"{node_id}:DESIGN:TestGenerator",
            label=self.agent_name,
            log_cb=self.log_cb,
        )
        tests = normalize_test_manifest_payload(raw_payload)
        output_text = json.dumps(raw_payload or {"tests": tests}, ensure_ascii=False)
        await self._log(f"Test generation returned {len(tests)} test artifact(s).", node_id=node_id)
        return tests, output_text

    async def _log(self, message: str, status: str | None = None, node_id: str | None = None) -> None:
        if self.log_cb is None:
            return
        result = self.log_cb(self.agent_name, message, status, node_id)
        if inspect.isawaitable(result):
            await result
