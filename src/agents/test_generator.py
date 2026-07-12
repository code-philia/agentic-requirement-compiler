from typing import Any, Dict, List

from .arc_agent import ARCAgent
from prompts.prompt_sections import (
    get_common_session_guidance,
    get_compiler_role_guidance,
    get_test_generator_guidance,
)


class TestGenerator(ARCAgent):
    def __init__(self, log_cb=None):
        super().__init__(
            agent_name="TestGenerator",
            log_cb=log_cb,
        )
        self._soft_read_guard: dict[str, int] = {}
        self._new_evidence_since_read = True

    def get_system_prompt(self) -> str:
        from core.utils import get_android_package, get_app_type, get_web_base_url, get_web_port

        app_type = get_app_type()
        if app_type == "android":
            android_pkg = get_android_package()
            pkg_dir = android_pkg.replace(".", "/")
            stack_rules = f"""
### Android test rules
- Unit, Integration, and E2E test packages must align with `{android_pkg}.unit`, `{android_pkg}.integration`, and `{android_pkg}.e2e`.
- Keep tests under `app/src/test/java/{pkg_dir}/...`.
- Stay JVM-only. Do not generate instrumentation or `androidTest` assets.
"""
        else:
            stack_rules = f"""
### Web test rules
- Unit and Integration tests use Vitest.
- E2E tests use Playwright under `backend/test-e2e/...`.
- Frontend Vitest tests stay under `frontend/tests/...`; backend Vitest tests stay under `backend/tests/...`.
- The only runtime base URL is `{get_web_base_url()}` on port `{get_web_port()}`.
- Do not invent a separate frontend runtime port.
- If a test uses the database, reuse `backend/src/database/` and create an isolated test DB through the scaffold instead of pointing tests at the default runtime database.
"""

        return f"""{get_compiler_role_guidance(
    role_name="TestGenerator",
    stage_name="test generation",
    mission=[
        "Your job is to turn the current node's declared interface contract into a compact, executable test suite.",
        "You do not invent product behavior. You derive assertions from the current requirement, scenarios, visual evidence, and the provided interfaces.",
        "You should minimize file count and keep tests aligned with real ownership boundaries so the implementation stage receives a coherent batch, not scattered fragments.",
    ],
    outputs=[
        "A compact coverage plan across the required unit, integration, and end-to-end layers for nodes that own executable verification assets.",
        "Executable test files with stable assertions and correct placement.",
        "A JSON manifest of the generated test artifacts for the current node.",
    ],
)}

Rules:
- Tests must cover `<interfaces>` and `<requirement_focus>`.
- Keep granularity coarse: prefer one primary file per layer, or one file per coherent scenario group.
- Prefer stable contract assertions over incidental DOM structure or implementation details.
- Treat the provided `<interfaces>` block as the source of truth for responsibility, specification, and test focus.
- Generate tests either per interface or per coherent scenario group, whichever yields fewer, more maintainable files.
- For the core owned path of a feature, prefer real collaborators and runtime wiring over mocks. Do not mock the very UI/API/FUNC/DB boundary the node is supposed to prove.
- If the requirement involves fetched, persisted, paginated, or user-submitted data, at least one Integration or E2E test must verify a real data loop such as request -> persistence -> response -> render, or submit -> write -> subsequent read.
- Do not let screenshot-derived sample values, fallback arrays, or placeholder content become the reason a test passes.
- If a generated test file is wrong, fix the file itself; do not rely on later environment hacks.
- Write test files first, then call `run_build` once to catch syntax and placement mistakes.

{get_common_session_guidance()}

{get_test_generator_guidance()}

{stack_rules}
"""

    def get_tool_names(self) -> List[str]:
        return [
            "read_file",
            "write_file",
            "edit_file",
            "delete_file",
            "list_directory",
            "glob",
            "grep",
            "run_build",
            "search_interfaces_by_keyword",
            "search_interfaces_by_relation",
            "get_node_relations",
        ]

    async def _intercept_tool_call(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        node_id: str | None = None,
    ) -> tuple[bool, Any]:
        if tool_name == "read_file":
            path = str(tool_args.get("path", "")).strip()
            if path:
                current_count = self._soft_read_guard.get(path, 0)
                if current_count >= 2 and not self._new_evidence_since_read:
                    return True, (
                        f"Soft stop: `{path}` has already been read multiple times in this test-design loop without new evidence. "
                        "Do not keep re-reading the same file from uncertainty alone. Either synthesize what you already learned and write the target test files, "
                        "or first gather genuinely new evidence with `grep`, `glob`, relation search, or a different owner/setup file."
                    )
                self._soft_read_guard[path] = current_count + 1
                self._new_evidence_since_read = False
            return False, None

        if tool_name in {"grep", "glob", "list_directory", "search_interfaces_by_keyword", "search_interfaces_by_relation", "get_node_relations"}:
            self._new_evidence_since_read = True
            return False, None

        if tool_name in {"write_file", "edit_file", "delete_file", "run_build"}:
            self._new_evidence_since_read = True
            return False, None

        return False, None

    @staticmethod
    def _build_scope_instruction(design_mode: str) -> str:
        if design_mode in {"non_leaf_ui_only", "non_leaf_full"}:
            return """
This parent-owned node does not register node-local test artifacts in this stage.
Return an empty JSON array and do not write test files.
"""
        return """
Generate Unit, Integration, and E2E coverage in one pass.
- Unit covers DB/FUNC contracts.
- Integration covers API and boundary collaboration.
- E2E covers the UI flows that are explicit in the requirement and scenarios.
- Integration and E2E must exercise the real owned chain instead of validating mocked success paths.
"""

    def build_initial_messages(
        self,
        node_id: str,
        requirement_data: Dict[str, Any],
        preloaded_source: str = None,
        design_mode: str = "leaf_full",
    ) -> tuple:
        from memory.context_pipeline import context_pipeline
        from tools import TOOL_REGISTRY

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
        )

        user_prompt = f"""
### Current Node Context
Read this first. The current requirement payload below is the authoritative task input for node `{node_id}`.
{dynamic_ctx}

### Task
{self._build_scope_instruction(design_mode)}

Additional rules:
- Cover the spec, not speculation.
- Use the provided requirement context plus `<interfaces>` blocks as the authoritative contract for ownership, visible literals, routes, messages, field labels, and test focus.
- Prefer one main test file per enabled layer, or one file per coherent scenario group when that is cleaner.
- Do not write tests that assert parent-owned behavior outside this node's scope.
- Keep the file count low and stable.
- For persisted or fetched data flows, write at least one test that would fail if the implementation used hardcoded sample data, mock API payloads, or placeholder panels instead of the real runtime chain.
- For owned web happy paths, prefer real API handlers and real DB-backed state in Integration/E2E tests; only mock systems outside this node's ownership boundary.
- If an Integration or E2E flow depends on persistence, make the test setup create or reset an isolated test database through the existing scaffold, then seed only the rows that the scenario actually needs.
- For DB-backed E2E flows, make the assertions meaningful across the full chain: browser action -> frontend request -> API -> DB write/read -> final UI state.
- For Playwright E2E selectors, prefer the most stable requirement-aligned locator available from `placeholder`, `label`, `name`, or `id`; use visible text or role only when they are clearly unique in the rendered page.
- If repeated text would make the E2E locator ambiguous, add stable local hooks such as `id`, `name`, or explicit label associations in the implementation and use them in the test.

When finished, output one JSON array in a `json` markdown block:
[
  {{
    "test_id": "unique id",
    "req_id": "{node_id}",
    "interface_ids": ["covered interfaces"],
    "type": "Unit|Integration|E2E",
    "file_path": "relative path",
    "first_line": "exact first line in the written test file"
  }}
]
"""
        system_content = self.get_system_prompt()
        if static_ctx:
            system_content = f"{system_content}\n\n{static_ctx}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]
        tools = [TOOL_REGISTRY[name]["schema"] for name in self.get_tool_names() if name in TOOL_REGISTRY]
        return messages, tools

    @staticmethod
    def _build_test_repair_prompt(node_id: str, last_error: str) -> str:
        error_line = f"The previous reply was rejected: {last_error}\n\n" if last_error else ""
        return f"""
{error_line}Your previous reply did not return a valid generated-test JSON array.

Do not read more files.
Do not call any tools.
Do not write or edit test files again in this repair turn.
Based on the requirement, designed interfaces, and the evidence you already gathered, return the final test manifest now.

Return exactly one JSON array in a ```json markdown block:
[
  {{
    "test_id": "unique id",
    "req_id": "{node_id}",
    "interface_ids": ["covered interfaces"],
    "type": "Unit|Integration|E2E",
    "file_path": "relative path",
    "first_line": "exact first line in the written test file"
  }}
]

Rules:
- Do not keep exploring.
- Every entry must be a JSON object with non-empty `test_id`, `type`, and `file_path`.
- Keep the file count low and aligned with the requirement scope for node `{node_id}`.
"""

    async def generate_tests_with_retry(
        self,
        node_id: str,
        requirement_data: Dict[str, Any],
        *,
        design_mode: str = "leaf_full",
        preloaded_source: str = None,
        validate: Any = None,
    ) -> tuple[list[dict[str, Any]] | None, list, str]:
        if design_mode in {"non_leaf_ui_only", "non_leaf_full", "skip"}:
            return [], [], "[]"

        from core.structured_output import run_agent_for_json_array

        messages, tools = self.build_initial_messages(
            node_id=node_id,
            requirement_data=requirement_data,
            preloaded_source=preloaded_source,
            design_mode=design_mode,
        )
        self._soft_read_guard = {}
        self._new_evidence_since_read = True
        return await run_agent_for_json_array(
            self,
            messages,
            node_id=node_id,
            max_steps=30,
            tools=tools,
            repair_prompt=lambda last_error: self._build_test_repair_prompt(node_id, last_error),
            log_cb=self.log_cb,
            log_agent=self.agent_name,
            validate=validate,
        )

    async def generate_tests(
        self,
        node_id: str,
        requirement_data: Dict[str, Any],
        preloaded_source: str = None,
        design_mode: str = "leaf_full",
    ) -> str:
        if design_mode in {"non_leaf_ui_only", "non_leaf_full", "skip"}:
            return "[]"

        messages, tools = self.build_initial_messages(
            node_id=node_id,
            requirement_data=requirement_data,
            preloaded_source=preloaded_source,
            design_mode=design_mode,
        )
        self._soft_read_guard = {}
        self._new_evidence_since_read = True
        result, _ = await self.run_from_messages(messages, node_id=node_id, max_steps=15, tools=tools)
        return result
