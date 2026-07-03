import json
import re
import os
from typing import List, Dict, Any, Awaitable, Callable
from .arc_agent import ARCAgent
from .codebase_explorer import CodebaseExplorer
from .test_failure_verifier import TestFailureVerifier
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
        self.codebase_explorer = CodebaseExplorer(log_cb)
        self.failure_verifier = TestFailureVerifier(log_cb)
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
        self._last_verifier_report: dict[str, Any] | None = None
        self._last_verifier_report_text: str = ""
        self._current_test_files: List[str] = []
        self._current_test_type: str = ""
        self._failure_history: list[dict[str, Any]] = []
        self._failure_attempt_counter = 0
        self._active_failure_phase: str = ""
        self._archived_failure_phase_summaries: list[dict[str, Any]] = []
        self._pending_post_verifier_compaction = False
        self._session_compaction_count = 0

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
        "Implementation on existing interfaces or new code for the current node.",
        "Passing results for the current target test batch.",
        "A final `IMPLEMENTED` only when the latest `run_tests` batch passes.",
    ],
)}

Land the current test batch by following the structured handoff: `<acceptance_gate>`, `<interfaces>`, `<test_file_cards>`, `<recent_failure_summary>`, `<requirement_focus>`, and any provided `<scenarios>` / `<visual_reference>`.

Rules:
- Implement the current node's declared contracts first. Do not invent a conflicting contract.
- Write the obvious implementation set first, then call `run_tests`.
- `run_tests` takes no arguments and runs exactly the current batch selected by the system.
- The node is only done after the current batch passes and the code remains buildable under final system verification.
- After a failed `run_tests`, use the latest failing output plus any injected independent failure-analysis report, then inspect only the next directly relevant files before the next edit or rerun.
- Make one minimal contract-preserving fix at a time, then verify again with `run_tests`.
- For features that own a UI -> API -> FUNC -> DB chain, make the real runtime path work. Do not satisfy the tests with sample rows, placeholder panels, mocked success branches, or fallback data that bypasses the owned path.
- If the feature or tests use the database, extend the scaffold under `backend/src/database/` for runtime queries, seed data, and isolated test databases instead of creating parallel DB lifecycle code.
- For Playwright E2E work, debug step by step: last passing browser step -> failing browser step -> frontend render/locator -> frontend API call -> backend route -> database side effect -> post-submit UI assertion.
- If an E2E or Integration flow depends on persistence, treat missing isolated test DB creation/reset/seed/write/read as a first-class root-cause candidate.
- Treat missing test discovery, wrong framework, wrong path, or wrong selector strategy as test/content/config problems first, not business-logic problems.
- Do not fabricate compatibility files, duplicate tests, patch `node_modules`, or move tests just to satisfy discovery.
- Treat the provided `<interfaces>` block as the source of truth for ownership, responsibility, specification, and test focus.
- If the requirement includes fetched or persisted data, assume the intended success condition is a real write/read or request/render loop unless the contract explicitly says otherwise.
- Return exactly `IMPLEMENTED` only after the latest `run_tests` result passes with exit code 0 and the implemented path is not relying on obvious sample-data or placeholder-only shortcuts for the owned flow.

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
        if self._last_run_tests_exit_code not in (None, 0):
            verifier_report = await self._run_failure_verifier_session(
                node_id=node_id,
                latest_test_output=result,
            )
            self._last_verifier_report = verifier_report
            self._last_verifier_report_text = self._format_verifier_report(verifier_report)
            self._append_failure_history_entry(result, verifier_report)
            self._pending_post_verifier_compaction = True
            self._forced_followup_user_messages.append(self._last_verifier_report_text)
        else:
            self._last_verifier_report = None
            self._last_verifier_report_text = ""
            self._pending_post_verifier_compaction = False
        return True, result

    async def _on_assistant_message_before_tool_calls(
        self,
        assistant_text: str,
        node_id: str | None = None,
    ) -> None:
        return None

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

    async def _postprocess_messages_after_tool_call(
        self,
        messages: List[Dict[str, Any]],
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_result: str,
        node_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        if tool_name != "run_tests" or not self._pending_post_verifier_compaction:
            return messages

        self._pending_post_verifier_compaction = False
        before_tokens = self._estimate_request_tokens(messages)
        compacted_messages = self._compact_session_after_verifier(messages)
        after_tokens = self._estimate_request_tokens(compacted_messages)
        self._session_compaction_count += 1
        await self._log(
            (
                f"Compacted TDD session after verifier completion "
                f"(#{self._session_compaction_count}, {before_tokens} -> {after_tokens} tokens)."
            ),
            node_id=node_id,
        )
        return compacted_messages

    async def _get_stop_response_before_final(
        self,
        final_response: str,
        node_id: str | None = None,
    ) -> str | None:
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
        previous_failure_history = list(self._failure_history)
        previous_failure_attempt_counter = self._failure_attempt_counter
        previous_active_failure_phase = self._active_failure_phase
        previous_archived_failure_phase_summaries = list(self._archived_failure_phase_summaries)
        previous_pending_post_verifier_compaction = self._pending_post_verifier_compaction
        previous_session_compaction_count = self._session_compaction_count

        self._run_tests_budget = run_tests_budget
        self._run_tests_usage = run_tests_usage if run_tests_usage is not None else {}
        self._stop_on_test_budget_exhausted = stop_on_test_budget_exhausted
        self._test_budget_exhausted = False
        self._run_tests_executor = run_tests_executor
        self._last_run_tests_exit_code = None
        self._last_run_tests_result = None
        self._last_completed_run_tests_result = None
        self._has_called_run_tests_in_session = False
        self._rejected_execute_commands = set()
        self._edit_read_required_paths = set()
        self._recent_failure_fingerprints = []
        self._forced_followup_user_messages = []
        self._last_verifier_report = None
        self._last_verifier_report_text = ""
        self._failure_history = []
        self._failure_attempt_counter = 0
        self._active_failure_phase = ""
        self._archived_failure_phase_summaries = []
        self._pending_post_verifier_compaction = False
        self._session_compaction_count = 0
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
            self._failure_history = previous_failure_history
            self._failure_attempt_counter = previous_failure_attempt_counter
            self._active_failure_phase = previous_active_failure_phase
            self._archived_failure_phase_summaries = previous_archived_failure_phase_summaries
            self._pending_post_verifier_compaction = previous_pending_post_verifier_compaction
            self._session_compaction_count = previous_session_compaction_count

    def get_last_run_tests_result(self) -> str | None:
        return self._last_completed_run_tests_result

    def get_last_verifier_report(self) -> str:
        return self._last_verifier_report_text

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

    def _append_failure_history_entry(
        self,
        latest_test_output: str,
        verifier_report: dict[str, Any],
    ) -> None:
        failure_phase = self._determine_failure_phase(latest_test_output, verifier_report)
        self._failure_attempt_counter += 1
        entry = {
            "attempt": self._failure_attempt_counter,
            "test_type": self._current_test_type or "",
            "test_files": list(self._current_test_files[:4]),
            "exit_code": self._extract_exit_code(latest_test_output),
            "fingerprint": self._recent_failure_fingerprints[-1] if self._recent_failure_fingerprints else "",
            "failure_phase": failure_phase,
            "failure_summary": str(verifier_report.get("failure_summary", "") or "").strip(),
            "likely_cause_summaries": [
                str(item.get("summary", "")).strip()
                for item in (verifier_report.get("likely_causes") or [])[:3]
                if isinstance(item, dict) and str(item.get("summary", "")).strip()
            ],
            "recommended_next_steps": [
                str(item).strip()
                for item in (verifier_report.get("recommended_next_steps") or [])[:3]
                if str(item).strip()
            ],
            "latest_test_output_excerpt": self._summarize_test_output(latest_test_output),
        }
        if self._current_test_type == "E2E":
            if self._active_failure_phase and failure_phase and failure_phase != self._active_failure_phase:
                archived = self._summarize_failure_phase(self._active_failure_phase, self._failure_history)
                if archived:
                    self._archived_failure_phase_summaries.append(archived)
                    self._archived_failure_phase_summaries = self._archived_failure_phase_summaries[-2:]
                self._failure_history = []
            self._active_failure_phase = failure_phase or self._active_failure_phase
            self._failure_history.append(entry)
            self._failure_history = self._failure_history[-2:]
            return

        self._failure_history.append(entry)
        self._failure_history = self._failure_history[-6:]

    @staticmethod
    def _summarize_test_output(tool_result: str, max_lines: int = 20) -> str:
        lines = [line.rstrip() for line in str(tool_result or "").splitlines() if line.strip()]
        if not lines:
            return ""
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
            lines.insert(0, "... [truncated]")
        return "\n".join(lines)

    @staticmethod
    def _extract_recent_tool_paths(
        messages: List[Dict[str, Any]],
        tool_names: set[str],
        limit: int = 8,
    ) -> list[str]:
        paths: list[str] = []
        for message in reversed(messages):
            if message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls") or []
            for tool_call in reversed(tool_calls):
                function_payload = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
                if function_payload.get("name") not in tool_names:
                    continue
                try:
                    args = json.loads(function_payload.get("arguments", "{}"))
                except Exception:
                    args = {}
                path = str(args.get("path", "")).strip()
                if path and path not in paths:
                    paths.append(path)
                if len(paths) >= limit:
                    return paths
        return paths

    def _compact_session_after_verifier(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(messages) < 2:
            return messages

        latest_failure = self._failure_history[-1] if self._failure_history else {}
        historical_failures = self._build_compact_historical_records()
        recent_read_files = self._extract_recent_tool_paths(messages, {"read_file"})
        recent_changed_files = self._extract_recent_tool_paths(
            messages,
            {"edit_file", "write_file", "delete_file"},
        )
        latest_verifier_report = self._last_verifier_report or {}
        compact_state = {
            "task_state": "TDD failure-recovery loop in progress for the current node.",
            "current_test_type": self._current_test_type or "",
            "target_test_files": list(self._current_test_files[:6]),
            "run_tests_usage": (
                self._run_tests_usage.get("run_tests", 0)
                if isinstance(self._run_tests_usage, dict)
                else 0
            ),
            "latest_failure": {
                "attempt": latest_failure.get("attempt"),
                "fingerprint": latest_failure.get("fingerprint", ""),
                "failure_phase": latest_failure.get("failure_phase", ""),
                "failure_summary": latest_failure.get("failure_summary", ""),
                "likely_cause_summaries": latest_failure.get("likely_cause_summaries", []),
                "recommended_next_steps": latest_failure.get("recommended_next_steps", []),
                "latest_test_output_excerpt": latest_failure.get("latest_test_output_excerpt", ""),
                "verifier_conclusion": latest_verifier_report,
            },
            "historical_failure_records": historical_failures,
            "recent_file_activity": {
                "read_files": recent_read_files,
                "changed_files": recent_changed_files,
            },
            "next_action": (
                latest_failure.get("recommended_next_steps", [])[0]
                if latest_failure.get("recommended_next_steps")
                else "Read the failing test and the nearest owner file before the next minimal fix."
            ),
        }
        compact_text = (
            "<tdd_session_compact_state>\n"
            f"{json.dumps(compact_state, indent=2, ensure_ascii=False)}\n"
            "</tdd_session_compact_state>"
        )
        continue_message = (
            "Continue from the compact TDD state above. Preserve the initial system prompt and the initial node-context "
            "user prompt as the source of truth. Treat historical failure records as background only. Prioritize the "
            "latest failure excerpt plus the verifier conclusion, focus on the current E2E failure phase when present, then inspect only the next directly relevant files "
            "before making one minimal fix."
        )
        return [
            messages[0],
            messages[1],
            {"role": "assistant", "content": compact_text},
            {"role": "user", "content": continue_message},
        ]

    @staticmethod
    def _build_initial_explorer_task(test_type: str) -> str:
        if test_type == "E2E":
            return (
                "Localize the smallest implementation-owner, route, page, API, runtime-boundary, and relevant "
                "test/setup file set for the current E2E batch. Prioritize the most likely edit targets and the next "
                "few files worth reading before the main TDD loop starts."
            )
        if test_type == "Integration":
            return (
                "Localize the smallest implementation-owner, boundary, and setup files for the current Integration "
                "test batch. Prioritize the most likely edit targets and the next few files worth reading."
            )
        return (
            "Localize the smallest implementation-owner and adjacent boundary files for the current Unit test batch. "
            "Prioritize the most likely edit targets and the next few files worth reading."
        )

    async def _run_initial_codebase_explorer(
        self,
        node_id: str,
        test_files: List[str],
        test_type: str,
        preloaded_source: str | None = None,
        previous_failure_summary: str = "",
        scope_note: str = "",
    ) -> dict[str, Any]:
        extra_context_parts: list[str] = []
        if test_files:
            extra_context_parts.append(
                "Target test files:\n" + json.dumps(test_files, indent=2, ensure_ascii=False)
            )
        if previous_failure_summary.strip():
            extra_context_parts.append(
                "Previous failure handoff:\n" + previous_failure_summary.strip()
            )
        if scope_note.strip():
            extra_context_parts.append(
                "Implementation scope guard:\n" + scope_note.strip()
            )
        focus_hints = [
            f"Current test type: {test_type}",
            "Prefer current-node owner files and the nearest failing boundary files.",
            "Surface the most likely edit targets before the TDD session starts editing.",
        ]
        return await self.codebase_explorer.explore(
            node_id=node_id,
            task_brief=self._build_initial_explorer_task(test_type),
            focus_hints=focus_hints,
            preloaded_source=preloaded_source,
            target_test_files=test_files,
            extra_context="\n\n".join(extra_context_parts),
            max_steps=8,
        )

    def build_initial_messages(
        self,
        node_id: str,
        test_files: List[str],
        test_type: str,
        preloaded_source: str = None,
        previous_failure_summary: str = "",
        scope_note: str = "",
        explorer_report: dict[str, Any] | None = None,
    ) -> tuple:
        """Build the [system, user] messages and tools list without calling run().
        Returns (messages, tools) so the caller can use run_from_messages() or continue a session.
        """
        from .context_pipeline import context_pipeline
        self._current_test_files = list(test_files)
        self._current_test_type = str(test_type or "").strip()

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
        scope_context = ""
        if scope_note:
            scope_context = f"\n### Implementation Scope Guard\n{scope_note}\n"
        explorer_context = ""
        if explorer_report:
            explorer_context = "\n" + CodebaseExplorer.format_report_block(explorer_report) + "\n"

        user_prompt = f"""
### Current Node Context
Read this first. The current requirement payload below is the authoritative task input for node `{node_id}`.
{dynamic_ctx}
{explorer_context}
{handoff_context}
{scope_context}

### Target Test Files
{json.dumps(test_files, indent=2)}

**Implementation Strategy**:
Implement the interfaces of the current node. Use the provided `<acceptance_gate>`, `<interfaces>`, `<test_file_cards>`, `<recent_failure_summary>`, and requirement context as the authoritative execution contract. Make the target tests pass without inventing a conflicting contract.
The system will execute exactly this current test batch when you call `run_tests`.
If `<codebase_explorer_report>` exists, use it as the initial file-localization map and only expand beyond it when direct evidence is missing or contradicted.
Treat the provided requirement context and `<interfaces>` as the source for explicit routes, visible text, field labels, placeholders, messages, API literals, and auth semantics unless the current test file proves they need repair.
Do not optimize for mocked green tests if the requirement expects a real runtime data flow. Prefer fixing the app code so the owned request, persistence, and render path actually works.
Use a simple loop: implement, run `run_tests`, use the latest failing output plus any injected verifier report, inspect the next directly relevant files, make one minimal fix, then rerun.
If this is an E2E batch, compare the failing Playwright spec with the raw Playwright output before deciding whether the minimal fix is in app code or the test file.
If this is a Playwright E2E batch, stable selectors may come from `placeholder`, `label`, `name`, or `id`; if repeated visible text is ambiguous, it is valid to add stable local hooks in the implementation and use them in the test.
If this is an E2E batch, classify the current failure phase first: `startup_or_environment`, `page_entry_or_render`, `locator_resolution`, `submit_runtime_path`, `post_submit_assertion`, or `other`.
If this is an E2E batch, debug it in this order unless the output already proves an earlier blocker:
1. Which Playwright step definitely passed last?
2. Which exact next step failed?
3. Did the frontend render the expected page, form, and controls for that step?
4. Is the locator/assertion targeting the correct control using stable hooks such as `placeholder`, `label`, `name`, or `id`?
5. If submit should happen, does the frontend call the correct API endpoint with the correct payload?
6. Does the backend route/controller perform the correct DB read/write?
7. Was an isolated test DB created/reset/seeded, and does it contain the expected data after the API call?
8. Does the post-submit UI state, message, redirect, or rendered data match what the test asserts?
If the failing flow depends on persistence and no isolated test DB exists yet, fix that through the existing scaffold before patching around the symptom in UI code.
When all target tests pass, output "IMPLEMENTED". The system will handle the final build verification after your batch is green.
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

    async def build_initial_messages_with_explorer(
        self,
        node_id: str,
        test_files: List[str],
        test_type: str,
        preloaded_source: str = None,
        previous_failure_summary: str = "",
        scope_note: str = "",
    ) -> tuple:
        explorer_report = await self._run_initial_codebase_explorer(
            node_id=node_id,
            test_files=test_files,
            test_type=test_type,
            preloaded_source=preloaded_source,
            previous_failure_summary=previous_failure_summary,
            scope_note=scope_note,
        )
        return self.build_initial_messages(
            node_id=node_id,
            test_files=test_files,
            test_type=test_type,
            preloaded_source=preloaded_source,
            previous_failure_summary=previous_failure_summary,
            scope_note=scope_note,
            explorer_report=explorer_report,
        )

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

    async def implement(
        self,
        node_id: str,
        test_files: List[str],
        test_type: str,
        preloaded_source: str = None,
        previous_failure_summary: str = "",
        scope_note: str = "",
    ) -> str:
        """Backwards-compatible: build initial messages then run a new session."""
        messages, tools = await self.build_initial_messages_with_explorer(
            node_id=node_id,
            test_files=test_files,
            test_type=test_type,
            preloaded_source=preloaded_source,
            previous_failure_summary=previous_failure_summary,
            scope_note=scope_note,
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

    async def _run_failure_verifier_session(
        self,
        node_id: str | None,
        latest_test_output: str,
    ) -> dict[str, Any]:
        node_label = node_id or "UNKNOWN_NODE"
        await self._log(
            "Launching independent failure-analysis session for the latest failing test batch.",
            node_id=node_id,
            agent_name="TestFailureVerifier",
        )
        messages, tools = self.failure_verifier.build_initial_messages(
            node_id=node_label,
            test_files=self._current_test_files,
            test_type=self._current_test_type or "Unknown",
            latest_test_output=latest_test_output,
        )
        report_text, _ = await self.failure_verifier.run_from_messages(
            messages=messages,
            node_id=node_id,
            max_steps=18,
            tools=tools,
        )
        parsed = self._extract_json_object(report_text or "")
        if isinstance(parsed, dict):
            if self._current_test_type == "E2E" and not str(parsed.get("failure_phase", "")).strip():
                parsed["failure_phase"] = self._classify_e2e_failure_phase(latest_test_output)
            return parsed
        return {
            "failure_phase": self._classify_e2e_failure_phase(latest_test_output) if self._current_test_type == "E2E" else "",
            "failure_summary": "Independent failure analysis returned non-JSON output.",
            "likely_causes": [
                {
                    "summary": "Unable to parse verifier result cleanly.",
                    "evidence": ["Raw verifier output was not valid JSON."],
                    "repair_options": ["Review the latest failing output and the failing test file directly."],
                }
            ],
            "requirement_alignment_notes": [],
            "test_asset_notes": [],
            "environment_notes": [],
            "implementation_notes": [],
            "recommended_next_steps": ["Read the failing test file, then inspect the nearest owner implementation file."],
            "confidence": "low",
        }

    @staticmethod
    def _format_verifier_report(report: dict[str, Any]) -> str:
        return (
            "<independent_failure_analysis>\n"
            "A separate read-only failure-analysis session has completed.\n"
            "Use this report as high-priority debugging context until direct file evidence disproves it.\n"
            "```json\n"
            f"{json.dumps(report, indent=2, ensure_ascii=False)}\n"
            "```\n"
            "</independent_failure_analysis>"
        )

    def _determine_failure_phase(
        self,
        latest_test_output: str,
        verifier_report: dict[str, Any],
    ) -> str:
        if self._current_test_type != "E2E":
            return ""
        report_phase = str(verifier_report.get("failure_phase", "") or "").strip()
        if report_phase:
            return report_phase
        return self._classify_e2e_failure_phase(latest_test_output)

    @staticmethod
    def _classify_e2e_failure_phase(latest_test_output: str) -> str:
        text = str(latest_test_output or "")
        lowered = text.lower()

        if (
            "frontend build failed before e2e startup" in lowered
            or "failed to start backend server for e2e testing" in lowered
            or "failed to start grouped e2e execution" in lowered
        ):
            return "startup_or_environment"

        if (
            "strict mode violation" in lowered
            or "waiting for getby" in lowered
            or "expect(locator)" in lowered
            or "locator resolved to" in lowered
            or "getbylabel(" in lowered
            or "getbyplaceholder(" in lowered
        ):
            return "locator_resolution"

        if (
            "tohaveurl(/\\/reserve" in lowered
            or "tohaveurl(/\\/login" in lowered
            or "tohaveurl(/\\/dashboard" in lowered
            or "getbytext('email is already registered.')" in lowered
            or "getbytext('passwords do not match.')" in lowered
            or "getbytext('please fill in all required fields.')" in lowered
            or "received string:  \"http://127.0.0.1:3000/register\"" in lowered
        ):
            return "post_submit_assertion"

        if (
            "page.goto(" in lowered
            or "net::err" in lowered
            or "tohaveurl(/\\/register" in lowered
        ):
            return "page_entry_or_render"

        if (
            "post /api/" in lowered
            or "status 500" in lowered
            or "status 400" in lowered
            or "status 409" in lowered
            or "request failed" in lowered
        ):
            return "submit_runtime_path"

        return "other"

    def _summarize_failure_phase(
        self,
        phase: str,
        entries: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not entries:
            return None
        latest = entries[-1]
        fingerprints = [
            str(item.get("fingerprint", "")).strip()
            for item in entries
            if str(item.get("fingerprint", "")).strip()
        ]
        return {
            "phase_summary": True,
            "failure_phase": phase,
            "attempts": [entries[0].get("attempt"), latest.get("attempt")],
            "fingerprints": fingerprints[-2:],
            "failure_summary": latest.get("failure_summary", ""),
            "likely_cause_summaries": latest.get("likely_cause_summaries", [])[:2],
        }

    def _build_compact_historical_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        records.extend(self._archived_failure_phase_summaries[-1:])
        if self._failure_history[:-1]:
            previous = self._failure_history[-2]
            records.append(
                {
                    "attempt": previous.get("attempt"),
                    "failure_phase": previous.get("failure_phase", ""),
                    "fingerprint": previous.get("fingerprint", ""),
                    "failure_summary": previous.get("failure_summary", ""),
                    "likely_cause_summaries": previous.get("likely_cause_summaries", [])[:2],
                }
            )
        return records[-2:]
