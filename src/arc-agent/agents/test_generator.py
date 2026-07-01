import json
from typing import Any, Dict, List

from .arc_agent import ARCAgent
from .prompt_sections import get_common_session_guidance, get_test_generator_guidance


class TestGenerator(ARCAgent):
    def __init__(self, log_cb=None):
        super().__init__(
            agent_name="TestGenerator",
            log_cb=log_cb,
        )
        self._soft_read_guard: dict[str, int] = {}
        self._new_evidence_since_read = True

    def get_system_prompt(self) -> str:
        from utils import get_android_package, get_app_type, get_web_base_url, get_web_port

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
"""

        return f"""You are the test design agent for this compiler.
Generate compact, executable test plans and test files that follow the interface spec instead of guessing behavior.

Rules:
- Tests must cover `<interface_spec>` and `<current_requirement>`.
- Keep granularity coarse: prefer one primary file per layer, or one file per coherent scenario group.
- Prefer stable contract assertions over incidental DOM structure or implementation details.
- Preserve `<frozen_node_contract>` and do not assert a conflicting route, auth behavior, or shell boundary.
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
    def _build_scope_instruction(design_mode: str, test_type: str) -> str:
        if design_mode == "non_leaf_ui_only":
            return """
This non-leaf node is UI-shell-only.
Do not generate test files. Return an empty JSON array: `[]`.
"""
        if design_mode == "non_leaf_ui_with_shell_tests":
            return """
This non-leaf node may only receive shell-level tests.
Only assert parent shell behavior such as route reachability, mount-point visibility, auth guard behavior, and top-level section boundaries.
Do not generate leaf business tests or parent-owned API/FUNC/DB tests.
Keep the file count minimal.
"""
        if test_type == "All":
            return """
Generate Unit, Integration, and E2E coverage in one pass.
- Unit covers DB/FUNC contracts.
- Integration covers API and boundary collaboration.
- E2E covers the UI flows that are explicit in the requirement and scenarios.
"""
        if test_type == "Unit+Integration":
            return """
Generate Unit and Integration coverage in one pass.
- Unit covers DB/FUNC contracts.
- Integration covers API and boundary collaboration.
- Do not generate E2E tests in this mode.
"""
        return f"Generate the `{test_type}` layer for this node."

    def build_initial_messages(
        self,
        node_id: str,
        requirement_data: Dict[str, Any],
        interfaces_ir: list,
        test_type: str = "Unit",
        preloaded_source: str = None,
        is_leaf: bool = True,
        node_understanding: dict[str, Any] | None = None,
        interface_spec: list[dict[str, Any]] | None = None,
        design_mode: str = "leaf_full",
    ) -> tuple:
        from .context_pipeline import context_pipeline
        from .tools import TOOL_REGISTRY

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
        )

        scenarios = requirement_data.get("scenarios") or []
        scenarios_context = ""
        if scenarios:
            scenarios_context = (
                "\n### Target Scenarios\n"
                f"{json.dumps(scenarios, indent=2, ensure_ascii=False)}\n"
            )

        understanding_context = ""
        if node_understanding:
            understanding_context = (
                "\n### Node Understanding\n```json\n"
                f"{json.dumps(node_understanding, indent=2, ensure_ascii=False)}\n```\n"
            )

        spec_context = ""
        if interface_spec:
            spec_context = (
                "\n### Interface Specification\n```json\n"
                f"{json.dumps(interface_spec, indent=2, ensure_ascii=False)}\n```\n"
            )

        ir_context = (
            "\n### Interface IR\n```json\n"
            f"{json.dumps(interfaces_ir, indent=2, ensure_ascii=False)}\n```\n"
        )

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}
{scenarios_context}
{understanding_context}
{spec_context}
{ir_context}

### Task
{self._build_scope_instruction(design_mode, test_type)}

Additional rules:
- Cover the spec, not speculation.
- Do not write tests that assert parent-owned behavior outside this node's scope.
- Keep the file count low and stable.
- For E2E selectors, prefer requirement-visible text first, then stable local selectors, then role, and use id only as a last fallback.

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

    async def generate_tests(
        self,
        node_id: str,
        requirement_data: Dict[str, Any],
        interfaces_ir: list,
        test_type: str = "Unit",
        preloaded_source: str = None,
        node_understanding: dict[str, Any] | None = None,
        interface_spec: list[dict[str, Any]] | None = None,
        design_mode: str = "leaf_full",
    ) -> str:
        messages, tools = self.build_initial_messages(
            node_id=node_id,
            requirement_data=requirement_data,
            interfaces_ir=interfaces_ir,
            test_type=test_type,
            preloaded_source=preloaded_source,
            node_understanding=node_understanding,
            interface_spec=interface_spec,
            design_mode=design_mode,
        )
        self._soft_read_guard = {}
        self._new_evidence_since_read = True
        result, _ = await self.run_from_messages(messages, node_id=node_id, max_steps=15, tools=tools)
        return result
