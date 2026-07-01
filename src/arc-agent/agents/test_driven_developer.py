import json
import re
import os
from typing import List, Dict, Any, Awaitable, Callable
from .arc_agent import ARCAgent
from .prompt_sections import (
    get_common_session_guidance,
    get_compiler_role_guidance,
    get_tdd_guidance,
)

class TestDrivenDeveloper(ARCAgent):
    def __init__(self, log_cb=None):
        super().__init__(
            agent_name="TestDrivenDeveloper",
            log_cb=log_cb
        )
        self._run_tests_budget: int | None = None
        self._run_tests_usage: Dict[str, int] | None = None
        self._stop_on_test_budget_exhausted = False
        self._test_budget_exhausted = False
        self._run_tests_executor: Callable[[], Awaitable[str]] | None = None
        self._last_run_tests_exit_code: int | None = None
        self._last_run_tests_result: str | None = None
        self._last_completed_run_tests_result: str | None = None
        self._has_called_run_tests_in_session = False
        self._rejected_execute_commands: set[str] = set()
        self._edit_read_required_paths: set[str] = set()
        self._recent_failure_fingerprints: list[str] = []
        self._forced_followup_user_messages: list[str] = []
        self._needs_failure_analysis = False

    @staticmethod
    def _has_required_failure_analysis_headers(text: str) -> bool:
        normalized = str(text or "").upper()
        required_headers = [
            "FAILURE_CLASSIFICATION",
            "ROOT_CAUSE_HYPOTHESIS",
            "TARGET_FILES",
        ]
        return all(header in normalized for header in required_headers)

    def get_system_prompt(self) -> str:
        return f"""{get_compiler_role_guidance(
    role_name="TestDrivenDeveloper",
    stage_name="test-driven implementation",
    mission=[
        "Your job is to take the current node's test batch and handoff artifacts, then land the minimal correct implementation that makes that batch pass.",
        "You implement against the declared contract from the design and test stages, not against guessed behavior.",
        "You work in a bounded fix-verify loop: form a hypothesis, inspect only the most relevant files, make the smallest change, then verify with `run_tests`.",
    ],
    outputs=[
        "Minimal contract-preserving code changes for the current node.",
        "Passing results for the current target test batch.",
        "A final `IMPLEMENTED` only when the latest `run_tests` batch passes.",
    ],
)}

Land the current test batch by following the structured handoff: `<interfaces>`, `<test_plan>`, `<test_code>`, `<requirement_focus>`, and any provided `<scenarios>` / `<visual_reference>`.

Rules:
- Implement the current node's declared contracts first. Do not invent a conflicting contract.
- Write the obvious implementation set first, then call `run_tests`.
- `run_tests` takes no arguments and runs exactly the current batch selected by the system.
- If tests fail, do not immediately read files or rerun tests.
- First send a short analysis with exactly these headings: `FAILURE_CLASSIFICATION`, `ROOT_CAUSE_HYPOTHESIS`, `TARGET_FILES`.
- `FAILURE_CLASSIFICATION` must be one of: `test_bug`, `selector_bug`, `wiring_bug`, `implementation_bug`.
- `ROOT_CAUSE_HYPOTHESIS` must be a concrete, falsifiable explanation tied to the latest failing output.
- `TARGET_FILES` must list the failing test file first, then at most two directly relevant code/config files.
- After that analysis: use `grep` to locate symbols, selectors, routes, or ownership boundaries; use `read_file` to confirm the current implementation text; use `edit_file` or `write_file` to make the smallest fix; use `run_tests` only to verify the hypothesis.
- Make the smallest contract-preserving fix before the next test run.
- Treat missing test discovery, wrong framework, wrong path, or wrong selector strategy as test/content/config problems first, not business-logic problems.
- Do not fabricate compatibility files, duplicate tests, patch `node_modules`, or move tests just to satisfy discovery.
- Treat the provided `<interfaces>` block as the source of truth for ownership, responsibility, specification, and test focus.
- Tool workflow:
- `grep` is for finding symbols, selectors, route ownership, and likely edit locations.
- `read_file` is for confirming the exact current implementation in files you already know are relevant.
- `run_tests` is only for verifying a concrete hypothesis after a minimal change.
- Avoid broad rescans, unrelated diagnostics, and repeated cached reads without a new hypothesis.
- Return exactly `IMPLEMENTED` only after the latest `run_tests` result passes with exit code 0.

{get_common_session_guidance()}

{get_tdd_guidance()}
"""

    def get_tool_names(self) -> List[str]:
        return ["read_file", "write_file", "edit_file", "delete_file", "list_directory", "glob", "grep",
                "execute_command", "run_tests", "run_build", "search_interfaces_by_keyword", "search_interfaces_by_relation", "get_node_relations"]

    @staticmethod
    def _validate_execute_command(command: str) -> str | None:
        normalized = re.sub(r"\s+", " ", str(command or "").strip()).lower()
        if not normalized:
            return "Rejected empty execute_command invocation."

        if any(token in normalized for token in ("vitest", "playwright test", "npm test", "pnpm test", "yarn test")):
            return (
                "Rejected execute_command test run. Use `run_tests` for the current target batch instead of "
                "manually invoking Vitest or Playwright."
            )

        if any(token in normalized for token in ("npm run build", "pnpm build", "yarn build", "vite build", "gradlew assemble", "gradlew test")):
            return (
                "Rejected execute_command build/test invocation. Use `run_build` for build verification and "
                "`run_tests` for the target test batch."
            )

        command_head = normalized.split(" ", 1)[0]
        if command_head in {"ls", "dir", "tree", "find", "rg", "grep", "cat", "type", "pwd"}:
            return (
                "Rejected execute_command repository exploration. Use `read_file`, `list_directory`, `glob`, or `grep` "
                "for codebase inspection."
            )

        return None

    async def _intercept_tool_call(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        node_id: str | None = None,
    ) -> tuple[bool, Any]:
        if self._needs_failure_analysis and tool_name in {"read_file", "run_tests"}:
            return True, (
                "Blocked by failure-analysis gate. The latest run_tests failed, so before any further `read_file` or "
                "`run_tests` you must first send a short analysis message with exactly these headings:\n"
                "FAILURE_CLASSIFICATION: test_bug | selector_bug | wiring_bug | implementation_bug\n"
                "ROOT_CAUSE_HYPOTHESIS: one concrete falsifiable explanation tied to the latest failing output\n"
                "TARGET_FILES: failing test file first, then at most two directly relevant files\n"
                "Then use `grep` if needed, `read_file` to confirm, and `run_tests` only after a minimal fix."
            )

        if tool_name == "execute_command":
            raw_command = str(tool_args.get("command", "")).strip()
            if raw_command in self._rejected_execute_commands:
                return True, (
                    "Rejected repeated execute_command invocation. This command was already rejected in the current "
                    "TDD session. Use the appropriate system tools or fix code directly."
                )
            validation_error = self._validate_execute_command(raw_command)
            if validation_error:
                if raw_command:
                    self._rejected_execute_commands.add(raw_command)
                return True, validation_error
            return False, None

        if tool_name == "edit_file":
            path = str(tool_args.get("path", "")).strip()
            if path and path in self._edit_read_required_paths:
                forced_context = self._consume_forced_followup_messages()
                return True, (
                    f"Rejected edit_file for `{path}` because a previous exact replacement failed. "
                    f"Do NOT retry from stale memory. Use the freshly injected file content below and issue a new minimal edit.\n\n"
                    f"{forced_context}"
                )
            return False, None

        if tool_name == "read_file":
            path = str(tool_args.get("path", "")).strip()
            if path:
                self._edit_read_required_paths.discard(path)
            return False, None

        if tool_name != "run_tests":
            return False, None

        usage = self._run_tests_usage if self._run_tests_usage is not None else {}
        used = usage.get("run_tests", 0)
        if self._run_tests_budget is not None and used >= self._run_tests_budget:
            self._test_budget_exhausted = True
            await self._log(
                f"[BUDGET] `run_tests` budget exhausted at {used}/{self._run_tests_budget}.",
                node_id=node_id,
            )
            return True, (
                f"Tool budget exhausted for `run_tests` ({used}/{self._run_tests_budget}). "
                "Stop calling this tool and summarize the current status."
            )

        used += 1
        usage["run_tests"] = used
        self._run_tests_usage = usage
        if self._run_tests_budget is not None:
            await self._log(
                f"[BUDGET] `run_tests` usage {used}/{self._run_tests_budget}.",
                node_id=node_id,
            )
        else:
            await self._log(
                f"[BUDGET] `run_tests` usage {used}.",
                node_id=node_id,
            )

        if self._run_tests_executor is None:
            return True, (
                "System test runner is not configured for this TDD session. "
                "Stop and report this execution blocker."
            )

        await self._log(
            "System is executing the current target test batch after the run_tests signal.",
            node_id=node_id,
        )
        result = await self._run_tests_executor()
        self._has_called_run_tests_in_session = True
        self._last_run_tests_result = result
        self._last_completed_run_tests_result = result
        self._last_run_tests_exit_code = self._extract_exit_code(result)
        self._record_failure_fingerprint(result)
        self._needs_failure_analysis = self._last_run_tests_exit_code not in (None, 0)
        return True, result

    async def _on_assistant_message_before_tool_calls(
        self,
        assistant_text: str,
        node_id: str | None = None,
    ) -> None:
        self._update_failure_analysis_state(assistant_text)

    async def _get_stop_response_after_tool_call(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_result: str,
        node_id: str | None = None,
    ) -> str | None:
        if (
            tool_name == "run_tests"
            and self._test_budget_exhausted
            and self._stop_on_test_budget_exhausted
        ):
            return "BUDGET_EXHAUSTED"
        return None

    async def _get_stop_response_before_final(
        self,
        final_response: str,
        node_id: str | None = None,
    ) -> str | None:
        self._update_failure_analysis_state(final_response)

        if (
            self._has_called_run_tests_in_session
            and self._last_run_tests_exit_code is not None
            and self._last_run_tests_exit_code != 0
        ):
            await self._log(
                "Rejected premature session completion because the most recent run_tests result was failing.",
                status="error",
                node_id=node_id,
            )
            if "IMPLEMENTED" in (final_response or "").upper():
                return (
                    "The latest run_tests result did not pass with Exit Code: 0, so you cannot declare IMPLEMENTED yet.\n"
                    "Read the most recent test output, trace the root cause by re-reading the failing test and the most relevant related files, then make the minimal correct fix and call run_tests again.\n"
                    "Do not fabricate compatibility test files or move tests just to satisfy discovery."
                )
            repeated_failure_note = ""
            if self._has_repeated_failure_loop():
                repeated_failure_note = (
                    "\nThe latest failing test fingerprint has repeated. Before another test rerun, "
                    "do not keep patching from stale memory. Re-read the current failing test file(s), then re-read the most relevant implementation/configuration file(s), "
                    "and only make one minimal contract-preserving fix after the root cause is evidenced."
                )
            repeated_failure_action = ""
            if self._has_repeated_failure_loop():
                repeated_failure_action = (
                    "\nNoise-control rule: avoid unrelated file reads, avoid `execute_command`, avoid broad rescans, "
                    "and do not rerun tests until you have inspected the latest test and implementation text."
                )
            return (
                "The latest run_tests result is still failing, so you cannot end this TDD session yet.\n"
                "Use the returned test output to decompose the failure, read the failing test and the next most relevant related files, then make the minimal code or test fix that is actually required and call run_tests again.\n"
                f"Do not stop at analysis only.{repeated_failure_note}{repeated_failure_action}"
            )

        if "IMPLEMENTED" not in (final_response or "").upper():
            return None
        if self._last_run_tests_exit_code == 0:
            return None

        await self._log(
            "Rejected premature IMPLEMENTED because the most recent run_tests result was not passing.",
            status="error",
            node_id=node_id,
        )
        return (
            "The latest run_tests result did not pass with Exit Code: 0, so you cannot declare IMPLEMENTED yet.\n"
            "Read the most recent test output, trace the root cause through the failing test and the most relevant related files, then make the minimal correct fix and call run_tests again.\n"
            "Do not fabricate compatibility test files or move tests just to satisfy discovery."
        )

    async def run_from_messages(
        self,
        messages: List[Dict[str, Any]],
        node_id: str = None,
        max_steps: int = 30,
        tools: List = None,
        run_tests_budget: int | None = None,
        run_tests_usage: Dict[str, int] | None = None,
        stop_on_test_budget_exhausted: bool = False,
        run_tests_executor: Callable[[], Awaitable[str]] | None = None,
    ) -> tuple:
        previous_budget = self._run_tests_budget
        previous_usage = self._run_tests_usage
        previous_stop = self._stop_on_test_budget_exhausted
        previous_exhausted = self._test_budget_exhausted
        previous_executor = self._run_tests_executor
        previous_last_exit_code = self._last_run_tests_exit_code
        previous_last_result = self._last_run_tests_result
        previous_last_completed_result = self._last_completed_run_tests_result
        previous_has_called_run_tests = self._has_called_run_tests_in_session

        self._run_tests_budget = run_tests_budget
        self._run_tests_usage = run_tests_usage if run_tests_usage is not None else {}
        self._stop_on_test_budget_exhausted = stop_on_test_budget_exhausted
        self._test_budget_exhausted = False
        self._run_tests_executor = run_tests_executor
        self._last_run_tests_exit_code = None
        self._last_run_tests_result = None
        self._last_completed_run_tests_result = None
        self._has_called_run_tests_in_session = False
        self._needs_failure_analysis = False
        self._rejected_execute_commands = set()
        self._edit_read_required_paths = set()
        self._recent_failure_fingerprints = []
        self._forced_followup_user_messages = []
        try:
            return await super().run_from_messages(
                messages=messages,
                node_id=node_id,
                max_steps=max_steps,
                tools=tools,
            )
        finally:
            latest_completed_result = self._last_completed_run_tests_result
            self._run_tests_budget = previous_budget
            self._run_tests_usage = previous_usage
            self._stop_on_test_budget_exhausted = previous_stop
            self._test_budget_exhausted = previous_exhausted
            self._run_tests_executor = previous_executor
            self._last_run_tests_exit_code = previous_last_exit_code
            self._last_run_tests_result = previous_last_result
            self._last_completed_run_tests_result = latest_completed_result or previous_last_completed_result
            self._has_called_run_tests_in_session = previous_has_called_run_tests

    def get_last_run_tests_result(self) -> str | None:
        return self._last_completed_run_tests_result

    def notify_edit_failure(self, path: str, tool_result: str) -> None:
        if not path or not isinstance(tool_result, str):
            return
        if "old_string not found" in tool_result.lower():
            self._edit_read_required_paths.add(path)
            self._forced_followup_user_messages.append(self._build_forced_edit_refresh(path))

    def _consume_forced_followup_messages(self) -> str:
        if not self._forced_followup_user_messages:
            return "No forced context available."
        payload = "\n\n".join(self._forced_followup_user_messages)
        self._forced_followup_user_messages = []
        return payload

    def drain_forced_followup_user_messages(self) -> list[str]:
        if not self._forced_followup_user_messages:
            return []
        messages = list(self._forced_followup_user_messages)
        self._forced_followup_user_messages = []
        return messages

    def _build_forced_edit_refresh(self, path: str) -> str:
        try:
            from utils import get_abs_path
            abs_path = get_abs_path(path)
            if not abs_path or not os.path.exists(abs_path):
                return (
                    f"<forced_file_refresh path=\"{path}\">\n"
                    "The file no longer exists or could not be re-read.\n"
                    "</forced_file_refresh>"
                )
            with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
            if len(content) > 5000:
                content = content[:5000] + "\n// ... [forced refresh truncated]"
            return (
                f"<forced_file_refresh path=\"{path}\">\n"
                f"{content}\n"
                "</forced_file_refresh>\n"
                "A previous exact replacement failed. Base the next edit on this refreshed file content and do not reuse the stale old_string."
            )
        except Exception as exc:
            return (
                f"<forced_file_refresh path=\"{path}\">\n"
                f"Failed to re-read file automatically: {exc}\n"
                "</forced_file_refresh>\n"
                "A previous exact replacement failed. Re-read the file logically before issuing another exact edit."
            )

    def build_initial_messages(
        self,
        node_id: str,
        test_files: List[str],
        test_type: str,
        preloaded_source: str = None,
        previous_failure_summary: str = "",
    ) -> tuple:
        """Build the [system, user] messages and tools list without calling run().
        Returns (messages, tools) so the caller can use run_from_messages() or continue a session.
        """
        from .context_pipeline import context_pipeline

        # 1. Use the new Context Pipeline to build layered context for the TDD Agent
        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
            target_test_files=test_files,
        )

        handoff_context = ""
        if previous_failure_summary:
            handoff_context = f"\n### Previous Failure Summary\n{previous_failure_summary}\n"

        user_prompt = f"""
### Current Node Context
Read this first. The current requirement payload below is the authoritative task input for node `{node_id}`.
{dynamic_ctx}
{handoff_context}

### Target Test Files
{json.dumps(test_files, indent=2)}

**Implementation Strategy**:
Implement the interfaces of the current node. Use the provided `<interfaces>`, `<test_plan>`, `<test_code>`, and requirement context as the authoritative execution contract. Make the target tests pass without inventing a conflicting contract.
The system will execute exactly this current test batch when you call `run_tests`.
If the batch fails, do not immediately read files or rerun tests. First output:
FAILURE_CLASSIFICATION: test_bug | selector_bug | wiring_bug | implementation_bug
ROOT_CAUSE_HYPOTHESIS: one concrete falsifiable explanation
TARGET_FILES: failing test file first, then at most two directly relevant files
Only after that analysis may you use `grep` to locate symbols, `read_file` to confirm the hypothesis, and `run_tests` to verify a minimal fix.
After each file read, reassess whether you already found the cause or whether you still need another directly related file.
If this is an E2E batch, compare the failing Playwright spec with the raw Playwright output before deciding whether the minimal fix is in app code or the test file.
When all target tests pass, output "IMPLEMENTED".
"""
        system_content = self.get_system_prompt()
        if static_ctx:
            system_content = f"{system_content}\n\n{static_ctx}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt}
        ]
        from .tools import TOOL_REGISTRY
        tools = [TOOL_REGISTRY[n]["schema"] for n in self.get_tool_names() if n in TOOL_REGISTRY]
        return messages, tools

    def _record_failure_fingerprint(self, tool_result: str) -> None:
        exit_code = self._extract_exit_code(tool_result)
        if exit_code == 0:
            self._recent_failure_fingerprints = []
            return
        lines = [line.strip() for line in str(tool_result or "").splitlines() if line.strip()]
        key_line = ""
        for line in lines:
            lowered = line.lower()
            if "error:" in lowered or "failed" in lowered or "no test files found" in lowered:
                key_line = line[:240]
                break
        fingerprint = f"{exit_code}|{key_line}"
        self._recent_failure_fingerprints.append(fingerprint)
        self._recent_failure_fingerprints = self._recent_failure_fingerprints[-4:]

    def _has_repeated_failure_loop(self) -> bool:
        if len(self._recent_failure_fingerprints) < 2:
            return False
        return self._recent_failure_fingerprints[-1] == self._recent_failure_fingerprints[-2]

    def _update_failure_analysis_state(self, assistant_text: str) -> None:
        if not self._needs_failure_analysis:
            return
        if self._has_required_failure_analysis_headers(assistant_text):
            self._needs_failure_analysis = False

    async def implement(
        self,
        node_id: str,
        test_files: List[str],
        test_type: str,
        preloaded_source: str = None,
        previous_failure_summary: str = "",
    ) -> str:
        """Backwards-compatible: build initial messages then run a new session."""
        messages, tools = self.build_initial_messages(
            node_id=node_id,
            test_files=test_files,
            test_type=test_type,
            preloaded_source=preloaded_source,
            previous_failure_summary=previous_failure_summary,
        )
        result, _ = await self.run_from_messages(
            messages,
            node_id=node_id,
            max_steps=50,
            tools=tools,
        )
        return result

    @staticmethod
    def _extract_exit_code(tool_result: str) -> int | None:
        if not isinstance(tool_result, str):
            return None
        for line in tool_result.splitlines():
            stripped = line.strip()
            if stripped.startswith("Exit Code:"):
                try:
                    return int(stripped.split("Exit Code:", 1)[1].strip())
                except ValueError:
                    return None
        return None
