import json
from typing import List, Dict, Any, Awaitable, Callable
from .arc_agent import ARCAgent

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

    def get_system_prompt(self) -> str:
        from utils import get_app_type, get_android_package, get_web_base_url, get_web_port
        app_type = get_app_type()
        # Keep a safe default so web mode does not crash when formatting shared prompt sections.
        android_pkg = "com.example.app"

        if app_type == "android":
            android_pkg = get_android_package()
            pkg_compliance = f"""
### Package Compliance (CRITICAL for Android):
- The application package is `{android_pkg}`. You MUST use this package for ALL generated code:
  - `package {android_pkg};` in every main source Java file
  - `import {android_pkg}.xxx;` for cross-module references
  - File paths must use `{android_pkg.replace('.', '/')}/` as the package directory
  - AndroidManifest.xml must reference activities as `{android_pkg}.ActivityName`
- For TEST code, use the correct sub-package matching the test type directory:
  - Unit tests: `package {android_pkg}.unit;`
  - Integration tests: `package {android_pkg}.integration;`
  - E2E tests: `package {android_pkg}.e2e;`
- Do NOT use `com.example.template` or any other package name.
- If the requirement description mentions a different package name in resource-id patterns, use THAT package name instead.
"""
        else:
            pkg_compliance = (
                "### Web Runtime And Port (CRITICAL):\n"
                f"- The single backend web port is `{get_web_port()}`.\n"
                f"- The backend serves the built frontend dist at `{get_web_base_url()}`.\n"
                "- Do not assume a separate frontend dev-server port such as `5173` or `5174` for deployment or E2E.\n"
                "- When fixing Playwright E2E tests, use `process.env.PLAYWRIGHT_BASE_URL` or the configured single-port base URL.\n"
            )

        return f"""You are an Elite Full-Stack Developer strictly following Test-Driven Development (TDD).
Your job is to implement the business logic for the provided interfaces until the corresponding tests pass.

Execution protocol (strict):
- Write ALL implementation files FIRST, THEN request a test run. Do NOT write one file and test immediately.
- The `run_tests` tool is only a signal. It takes NO arguments. When you call it, the system will execute the current target test batch and return the results in the tool output.
- If tests fail, read the returned error output carefully, use `edit_file` to fix the minimal set of issues (provide exact old_string/new_string), and then call `run_tests` again.
- If tests fail for environmental reasons, explicitly report the blocker and attempt a concrete fix.
- Treat `No test files found` and equivalent discovery errors as runner/path/config problems first. Do not start by changing business logic when the runner did not actually execute the target test file.
- Do not create mirror test files, proxy test files, duplicate test files, or path-compatibility files such as `backend/backend/...` just to satisfy a broken runner path.
- Do not copy or relocate tests only to make discovery pass unless the system output explicitly shows the real project configuration itself must be fixed.
- Do not copy dependencies between `backend` and `frontend`, do not vendor packages manually, and do not patch `node_modules` or package internals to make tests pass.
- Do not write ad hoc Vitest or Playwright config files unless the real project configuration is genuinely missing and the system output shows that configuration is the blocker.
- If a generated test file uses the wrong framework for its type, treat that as a test-content bug. Do not compensate by rewriting the runtime environment around it.
- E2E tests must be Playwright tests. They are not Vitest tests, and you must not reinterpret E2E failures through `require('vitest')`, `describe/it/expect`-only assumptions, or rewrite E2E into Vitest unless the requirement explicitly changes frameworks.
- If an E2E file itself appears to be Vitest-style, treat that as a test-generation or test-content error to be corrected. Do not "fix" it by changing the runner away from Playwright.
- When E2E fails, debug it as Playwright: page interaction, selectors, assertions, server startup, and runtime environment.
- For web projects, Playwright E2E spec files belong under `backend/test-e2e/...`. Do not move them to `frontend/src/...` or root-level `e2e/...`.
- For web projects, backend tests and frontend tests may run from different working directories. Use the `Working Directory` and `Resolved Test File` fields from system output as the source of truth before deciding whether the blocker is path, config, test content, or implementation.
- The current node already has a concrete interface list. Implement those interfaces first and keep them aligned with their declared file path, signature, callers, callees, inputs, and outputs.
- Use the provided `<project_structure>` as the default source of truth for file and directory locations. Do not start by probing guessed sibling directories.
- If you discover that passing tests requires a genuinely new interface that was not in the original node IR, you may add it. In that case, include a JSON array in a markdown `json` block in your final response describing only the newly added interfaces using the same schema as interface design. Do not emit this JSON for ordinary code edits.
- Return exactly "IMPLEMENTED" only when target tests are truly passing.

{pkg_compliance}
### Workflow:
1. Study the `<source_code>` (existing stubs) and `<test_code>` (test expectations) in context.
2. For each interface of the current node, write the REAL implementation that satisfies its Outputs contract and makes the corresponding tests pass.
3. Use `write_file` to write ALL implementation files in one batch.
4. Call `run_tests` with NO arguments to ask the system to execute the current target test batch.
5. If tests fail, read the returned error output, fix the code, and call `run_tests` again.
6. If you need a new npm package, use `execute_command` (e.g., `npm install cors`).

Once `run_tests` returns a 100% passing state (Exit Code: 0) for the target tests, you MUST output exactly the word "IMPLEMENTED" in your final response to complete the task.
"""

    def get_tool_names(self) -> List[str]:
        return ["read_file", "write_file", "edit_file", "delete_file", "list_directory", "glob", "grep",
                "execute_command", "run_tests", "run_build", "search_interfaces_by_keyword", "search_interfaces_by_relation", "get_node_relations"]

    async def _intercept_tool_call(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        node_id: str | None = None,
    ) -> tuple[bool, Any]:
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
        return True, result

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
                    "Read the most recent test output, fix the real issue, and call run_tests again.\n"
                    "Do not fabricate compatibility test files or move tests just to satisfy discovery."
                )
            return (
                "The latest run_tests result is still failing, so you cannot end this TDD session yet.\n"
                "Use the returned test output to make the minimal code or test fix that is actually required, then call run_tests again.\n"
                "Do not stop at analysis only."
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
            "Read the most recent test output, fix the real issue, and call run_tests again.\n"
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

    def build_initial_messages(
        self,
        node_id: str,
        test_files: List[str],
        test_type: str,
        req_desc: str,
        scenarios: list = None,
        dependency_context: str = "",
        current_interfaces: list = None,
        preloaded_source: str = None,
        handoff_summary: str = "",
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

        scenarios_context = ""
        if test_type == "E2E" and scenarios:
            scenarios_context = f"\n### Target UI Scenarios\n{json.dumps(scenarios, indent=2, ensure_ascii=False)}"

        interfaces_context = ""
        if current_interfaces:
            interface_lines = []
            for interface in current_interfaces:
                if not isinstance(interface, dict):
                    continue
                interface_lines.append(
                    json.dumps(
                        {
                            "interface_id": interface.get("interface_id", ""),
                            "type": interface.get("type", ""),
                            "file_path": interface.get("file_path", ""),
                            "first_line": interface.get("first_line", ""),
                            "implemented": interface.get("implemented", False),
                            "content": interface.get("content", ""),
                        },
                        ensure_ascii=False,
                    )
                )
            if interface_lines:
                interfaces_context = "\n### Current Node Interfaces\n" + "\n".join(interface_lines)

        handoff_context = ""
        if handoff_summary:
            handoff_context = f"\n### Stage Handoff Summary\n{handoff_summary}\n"

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}
{scenarios_context}
{interfaces_context}
{handoff_context}

### Target Test Files
{json.dumps(test_files, indent=2)}

**Implementation Strategy**:
Implement the interfaces of the current node. Consider the node requirement content, each interface's responsibility, and its spec to decide what to implement. Make the target tests pass.
The system will execute exactly this current test batch when you call `run_tests`.
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

    async def implement(self, node_id: str, test_files: List[str], test_type: str, req_desc: str, scenarios: list = None, dependency_context: str = "", current_interfaces: list = None, preloaded_source: str = None, handoff_summary: str = "") -> str:
        """Backwards-compatible: build initial messages then run a new session."""
        messages, tools = self.build_initial_messages(
            node_id=node_id, test_files=test_files, test_type=test_type,
            req_desc=req_desc, scenarios=scenarios, dependency_context=dependency_context,
            current_interfaces=current_interfaces, preloaded_source=preloaded_source,
            handoff_summary=handoff_summary,
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
