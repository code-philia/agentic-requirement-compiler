import json
from typing import List, Dict, Any
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

    def get_system_prompt(self) -> str:
        from utils import get_app_type, get_android_package
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
            pkg_compliance = ""

        return f"""You are an Elite Full-Stack Developer strictly following Test-Driven Development (TDD).
Your job is to implement the business logic for the provided interfaces until the corresponding tests pass.

Execution protocol (strict):
- Write ALL implementation files FIRST, THEN run tests. Do NOT write one file and test immediately.
- If tests fail, read the error output carefully, use `edit_file` to fix the minimal set of issues (provide exact old_string/new_string), and rerun `run_tests`.
- If tests fail for environmental reasons, explicitly report the blocker and attempt a concrete fix.
- Return exactly "IMPLEMENTED" only when target tests are truly passing.

{pkg_compliance}
### Workflow:
1. Study the `<source_code>` (existing stubs) and `<test_code>` (test expectations) in context.
2. For each interface of the current node, write the REAL implementation that satisfies its Outputs contract and makes the corresponding tests pass.
3. Use `write_file` to write ALL implementation files in one batch.
4. Call `run_tests` with the target test type to verify (Green phase).
5. If tests fail, read the error output, fix the code, and rerun `run_tests`.
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
        if tool_name != "run_tests" or self._run_tests_budget is None:
            return False, None

        usage = self._run_tests_usage if self._run_tests_usage is not None else {}
        used = usage.get("run_tests", 0)
        if used >= self._run_tests_budget:
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
        await self._log(
            f"[BUDGET] `run_tests` usage {used}/{self._run_tests_budget}.",
            node_id=node_id,
        )
        return False, None

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

    async def run_from_messages(
        self,
        messages: List[Dict[str, Any]],
        node_id: str = None,
        max_steps: int = 30,
        tools: List = None,
        run_tests_budget: int | None = None,
        run_tests_usage: Dict[str, int] | None = None,
        stop_on_test_budget_exhausted: bool = False,
    ) -> tuple:
        previous_budget = self._run_tests_budget
        previous_usage = self._run_tests_usage
        previous_stop = self._stop_on_test_budget_exhausted
        previous_exhausted = self._test_budget_exhausted

        self._run_tests_budget = run_tests_budget
        self._run_tests_usage = run_tests_usage if run_tests_usage is not None else {}
        self._stop_on_test_budget_exhausted = stop_on_test_budget_exhausted
        self._test_budget_exhausted = False
        try:
            return await super().run_from_messages(
                messages=messages,
                node_id=node_id,
                max_steps=max_steps,
                tools=tools,
            )
        finally:
            self._run_tests_budget = previous_budget
            self._run_tests_usage = previous_usage
            self._stop_on_test_budget_exhausted = previous_stop
            self._test_budget_exhausted = previous_exhausted

    def build_initial_messages(self, node_id: str, test_files: List[str], test_type: str, req_desc: str, scenarios: list = None, dependency_context: str = "", current_interfaces: list = None, preloaded_source: str = None) -> tuple:
        """Build the [system, user] messages and tools list without calling run().
        Returns (messages, tools) so the caller can use run_from_messages() or continue a session.
        """
        from .context_pipeline import context_pipeline

        # 1. Use the new Context Pipeline to build layered context for the TDD Agent
        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id, agent_type=self.agent_name, preloaded_source=preloaded_source
        )

        scenarios_context = ""
        if test_type == "E2E" and scenarios:
            scenarios_context = f"\n### Target UI Scenarios\n{json.dumps(scenarios, indent=2, ensure_ascii=False)}"

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}
{scenarios_context}

### Target Test Files
{json.dumps(test_files, indent=2)}

**Implementation Strategy**:
Implement the interfaces of the current node. Consider the node requirement content, each interface's responsibility, and its spec to decide what to implement. Make the target tests pass.
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

    async def implement(self, node_id: str, test_files: List[str], test_type: str, req_desc: str, scenarios: list = None, dependency_context: str = "", current_interfaces: list = None, preloaded_source: str = None) -> str:
        """Backwards-compatible: build initial messages then run a new session."""
        messages, tools = self.build_initial_messages(
            node_id=node_id, test_files=test_files, test_type=test_type,
            req_desc=req_desc, scenarios=scenarios, dependency_context=dependency_context,
            current_interfaces=current_interfaces, preloaded_source=preloaded_source
        )
        result, _ = await self.run_from_messages(messages, node_id=node_id, max_steps=15, tools=tools)
        return result
