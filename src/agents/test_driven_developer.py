from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from agents.context.pipeline import context_pipeline
from agents.context.global_memory import GlobalProjectMemory
from agents.context.prompts.common import stage_skill_activation_policy
from agents.context.prompts.test_driven_developer import get_system_prompt, get_user_prompt
from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.factory import build_stage_agent
from agents.runtime.runners import ainvoke_stage_agent
from agents.skills.selection import SKILLS_SOURCE, implementation_skills
from agents.tools.build import build_run_build_tool as build_system_run_build_tool
from agents.tools.traceability import build_traceability_tools


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


class TestDrivenDeveloper:
    """Deep-agents based TDD implementation adapter."""

    agent_name = "TestDrivenDeveloper"

    def __init__(
        self,
        log_cb: LogCallback | None = None,
        *,
        model: str | object | None = None,
        workspace_root: str | None = None,
        requirement_path: str | None = None,
        app_type: str | None = None,
        app_handler: Any | None = None,
    ) -> None:
        self.log_cb = log_cb
        self.model = model or os.environ.get("MODEL", "openai:gpt-5.4")
        self.workspace_root = workspace_root
        self.requirement_path = requirement_path or ""
        self.app_type = app_type
        self.app_handler = app_handler
        self._last_run_tests_result: str | None = None
        self._last_run_tests_exit_code: int | None = None
        self._last_verifier_report_text = ""
        self._test_budget_exhausted = False
        self._current_test_files: list[str] = []
        self._current_test_type = ""

    async def run(
        self,
        *,
        node_id: str,
        test_files: list[str],
        test_type: str,
        node_tests: list[dict[str, Any]] | None = None,
        preloaded_source: str | None = None,
        previous_failure_summary: str = "",
        run_tests_budget: int | None = None,
        run_tests_usage: dict[str, int] | None = None,
        stop_on_test_budget_exhausted: bool = True,
        run_tests_executor: Callable[[str | None, list[str] | None], Awaitable[str]] | None = None,
        protect_test_files: bool = False,
        continue_across_layers: bool = False,
    ) -> str:
        self._last_run_tests_result = None
        self._last_run_tests_exit_code = None
        self._last_verifier_report_text = ""
        self._test_budget_exhausted = False
        self._current_test_files = [str(path or "").strip() for path in test_files if str(path or "").strip()]
        self._current_test_type = test_type
        current_node_tests = [item for item in (node_tests or []) if isinstance(item, dict)]
        workspace_root = str(Path(
            self.workspace_root
            or context_pipeline.config.workspace_dir
            or os.environ.get("ARC_WORKSPACE_ROOT")
            or os.getcwd()
        ).expanduser().resolve())
        app_type = (self.app_type or context_pipeline.config.app_type or os.environ.get("ARC_APP_TYPE") or "web").strip().lower()
        write_latch = {"passed": False, "budget_exhausted": False, "advance_required": False}

        def normalize_requested_path(value: Any) -> str:
            path = str(value or "").strip().replace("\\", "/")
            if path.startswith("/workspace/"):
                return path[len("/workspace/") :].lstrip("/")
            if path == "/workspace":
                return ""
            return path.lstrip("./")

        context_pipeline.configure(
            workspace_dir=workspace_root,
            app_type=app_type,
        )
        static_context, dynamic_context = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
            target_test_files=self._current_test_files,
        )
        interface_contract = context_pipeline.get_interface_contract_context(node_id)
        context_text = "\n\n".join(part.strip() for part in (static_context, dynamic_context) if part.strip())
        selected_skill_names = implementation_skills(
            interface_contract=interface_contract,
            previous_failure_summary=previous_failure_summary,
        )

        async def run_tests(test_type: str | None = None, test_files: list[str] | None = None) -> str:
            """Run current-node tests. Optionally pass a test_type or exact test_files from the manifest."""

            if write_latch["passed"] and self._last_run_tests_result:
                return (
                    self._last_run_tests_result
                    + "\n\nARC_STOP_CONDITION:\n"
                    + "- This layer already passed. No additional test execution or file edit is allowed.\n"
                    + "- Return exactly IMPLEMENTED now.\n"
                )

            requested_type = str(
                test_type
                or ("" if continue_across_layers else self._current_test_type)
            ).strip()
            requested_files = [
                path
                for value in (test_files or [])
                if (path := normalize_requested_path(value))
            ]
            usage = run_tests_usage if run_tests_usage is not None else None
            if usage is not None or run_tests_budget is not None:
                usage = usage if usage is not None else {}
                used = int(usage.get("run_tests", 0))
                if run_tests_budget is not None and used >= run_tests_budget:
                    self._test_budget_exhausted = True
                    await self._log(
                        f"`run_tests` budget exhausted at {used}/{run_tests_budget}.",
                        status="error",
                        node_id=node_id,
                    )
                    return (
                        f"Tool budget exhausted for `run_tests` ({used}/{run_tests_budget}). "
                        "Stop and summarize the current status."
                    )
                usage["run_tests"] = used + 1
                await self._log(
                    f"`run_tests` usage {usage['run_tests']}" + (f"/{run_tests_budget}" if run_tests_budget is not None else "") + ".",
                    node_id=node_id,
                )
            if requested_files and test_type is None:
                file_types = {
                    str(item.get("type", "") or "").strip()
                    for item in current_node_tests
                    if str(item.get("file_path", "") or "").strip() in requested_files
                    and str(item.get("type", "") or "").strip()
                }
                if len(file_types) == 1:
                    requested_type = next(iter(file_types))
                elif len(file_types) > 1:
                    return (
                        "Exit Code: 1\n"
                        "STDERR:\n"
                        "run_tests(test_files=[...]) received files from multiple test types; pass one type at a time.\n"
                    )
            if not requested_files and test_type:
                requested_files = [
                    str(item.get("file_path", "") or "").strip()
                    for item in current_node_tests
                    if str(item.get("type", "") or "").strip() == requested_type
                    and str(item.get("file_path", "") or "").strip()
                ]
                if not requested_files:
                    return (
                        "Exit Code: 1\n"
                        "STDERR:\n"
                        f"No current-node tests are registered for test_type={requested_type!r}.\n"
                    )
            result = (
                "Exit Code: 1\n"
                "STDERR:\n"
                "System test runner is not configured for this TDD session.\n"
            ) if run_tests_executor is None else await run_tests_executor(requested_type, requested_files or None)
            self._last_run_tests_result = result
            self._last_run_tests_exit_code = self._extract_exit_code(result)
            batch_match = re.search(r"Batch Test Type:\s*(Unit|Integration|E2E)", result, re.IGNORECASE)
            effective_type = batch_match.group(1).title() if batch_match else requested_type or self._current_test_type
            if effective_type.upper() == "E2E":
                effective_type = "E2E"
            effective_files = requested_files or [
                str(item.get("file_path", "") or "").strip()
                for item in current_node_tests
                if str(item.get("type", "") or "").strip().casefold() == effective_type.casefold()
                and str(item.get("file_path", "") or "").strip()
            ]
            self._current_test_type = effective_type
            write_latch["passed"] = self._last_run_tests_exit_code == 0 and (
                not continue_across_layers or "ARC_IMPLEMENT_SESSION_COMPLETE" in result
            )
            write_latch["advance_required"] = "ARC_TEST_LAYER_BUDGET_EXHAUSTED_CONTINUE" in result
            write_latch["budget_exhausted"] = (
                "ARC_TEST_BUDGET_EXHAUSTED" in result and not write_latch["advance_required"]
            )
            self._record_failure_state(result)
            store = context_pipeline._store()
            if store is not None:
                GlobalProjectMemory(workspace_root, store).record_test_result(
                    requirement=node_id,
                    test_type=effective_type,
                    test_files=effective_files,
                    output=result,
                )
            return result

        if self.app_handler is None:
            async def system_run_build() -> str:
                """Run system-defined build verification when an app handler is configured."""

                return (
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    "System build runner is not configured for this TDD session.\n"
                )
        else:
            system_run_build = build_system_run_build_tool(
                app_handler=self.app_handler,
                node_id=node_id,
                log_cb=self.log_cb,
            )

        async def run_build() -> str:
            """Run build verification and retain actionable compiler failures."""

            result = await system_run_build()
            store = context_pipeline._store()
            if store is not None:
                GlobalProjectMemory(workspace_root, store).record_test_result(
                    requirement=node_id,
                    test_type="Build",
                    test_files=[],
                    output=result,
                )
            return result

        traceability_tools = build_traceability_tools(node_id=node_id, log_cb=self.log_cb)
        agent = build_stage_agent(
            name="test_driven_developer",
            stage="implementation",
            model=self.model,
            system_prompt="\n\n".join(
                [get_system_prompt(), stage_skill_activation_policy(selected_skill_names)]
            ),
            response_format=None,
            workspace_root=workspace_root,
            writable_roots=[workspace_root],
            skills=[SKILLS_SOURCE] if selected_skill_names else [],
            permitted_skill_names=selected_skill_names,
            memory=[],
            tools=[run_tests, run_build, *traceability_tools],
            protected_write_paths=self._current_test_files if protect_test_files else None,
            write_block_reason=lambda: (
                "The active test layer has already passed. Stop without further edits and return IMPLEMENTED."
                if write_latch["passed"]
                else (
                    "The prior layer exhausted its budget and the system advanced. Call run_tests now for the next layer before editing."
                    if write_latch["advance_required"]
                    else (
                        "The final test layer exhausted its execution budget. Stop editing and return the required continuation handoff."
                        if write_latch["budget_exhausted"]
                        else None
                    )
                )
            ),
        )
        has_work_packet = '"work_packets":[' in context_text and '"work_packets":[]' not in context_text
        message = get_user_prompt(
            node_id=node_id,
            dynamic_context=context_text,
            interface_contract=interface_contract,
            test_files=self._current_test_files,
            test_type=test_type,
            node_tests=current_node_tests,
            previous_failure_summary=(
                ""
                if has_work_packet
                else previous_failure_summary
            ),
            run_tests_budget=run_tests_budget,
            protect_test_files=protect_test_files,
            has_work_packet=has_work_packet,
        )
        await self._log(f"skill-permitted: {', '.join(selected_skill_names) or 'none'}", node_id=node_id)
        await self._log("Invoking TDD implementation.", node_id=node_id)
        payload = await ainvoke_stage_agent(
            agent,
            message=message,
            context=AgentRuntimeContext(
                node_id=node_id,
                phase="IMPLEMENT",
                app_type=app_type,
                workspace_root=workspace_root,
                requirement_path=self.requirement_path,
                test_type=self._current_test_type,
            ),
            thread_id=f"{node_id}:IMPLEMENT:TestDrivenDeveloper:{self._current_test_type or 'batch'}",
            label=self.agent_name,
            log_cb=self.log_cb,
        )
        final_text = self._payload_to_final_text(payload)
        if self._test_budget_exhausted and stop_on_test_budget_exhausted:
            return "BUDGET_EXHAUSTED"
        if "IMPLEMENTED" in final_text.upper() and self._last_run_tests_exit_code != 0:
            final_text = (
                "Error: latest run_tests result did not pass with Exit Code: 0, "
                "so IMPLEMENTED is not accepted for this batch."
            )
        await self._log("TDD session completed.", node_id=node_id)
        return final_text

    def get_last_run_tests_result(self) -> str | None:
        return self._last_run_tests_result

    def get_last_verifier_report(self) -> str:
        return self._last_verifier_report_text

    @staticmethod
    def _payload_to_final_text(payload: dict[str, Any]) -> str:
        summary = str(payload.get("summary", "") or "").strip()
        if summary:
            return summary
        for key in ("final", "result", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _record_failure_state(self, result: str) -> None:
        exit_code = self._extract_exit_code(result)
        if exit_code == 0:
            self._last_verifier_report_text = ""
            return
        key_line = ""
        for line in (result or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if "error" in lowered or "failed" in lowered or "expect" in lowered:
                key_line = stripped[:240]
                break
        lines = [line for line in (result or "").splitlines() if line.strip()]
        excerpt = "\n".join(lines[-40:])
        self._last_verifier_report_text = (
            "<failure_analysis>\n"
            f"fingerprint: {exit_code}|{key_line}\n"
            "latest_test_output_excerpt:\n"
            f"{excerpt}\n"
            "</failure_analysis>"
        )

    @staticmethod
    def _extract_exit_code(tool_result: str) -> int | None:
        for line in (tool_result or "").splitlines():
            stripped = line.strip()
            if not stripped.startswith("Exit Code:"):
                continue
            try:
                return int(stripped.split("Exit Code:", 1)[1].strip())
            except ValueError:
                return None
        return None

    async def _log(self, message: str, status: str | None = None, node_id: str | None = None) -> None:
        if self.log_cb is None:
            return
        result = self.log_cb(self.agent_name, message, status, node_id)
        if inspect.isawaitable(result):
            await result
