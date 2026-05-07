import json
from typing import List, Dict, Any
from .arc_agent import ARCAgent

class TestGenerator(ARCAgent):
    def __init__(self, broadcast_cb=None):
        super().__init__(
            agent_name="TestGenerator",
            broadcast_cb=broadcast_cb
        )

    def get_system_prompt(self) -> str:
        from utils import get_app_type
        app_type = get_app_type()

        if app_type == "android":
            test_stack = """
# Testing Stack (Android):
- **Unit Tests**: JUnit5 + Robolectric — place in `app/src/test/java/<package>/`
  - Use `@Test` from `org.junit.jupiter.api.Test`
  - Use Robolectric `@RunWith(RobolectricTestRunner.class)` when Android context is needed
  - Target `FUNC` and `DB` interfaces
- **Integration Tests**: Robolectric + MockWebServer + Room in-memory DB — place in `app/src/test/java/<package>/`
  - Use MockWebServer for API/HTTP testing
  - Use Room in-memory DB (`Room.inMemoryDatabaseBuilder`) for DB integration testing
  - Target `API` interfaces
- **E2E Tests**: Espresso — place in `app/src/androidTest/java/<package>/`
  - Use `@RunWith(AndroidJUnit4.class)` + Espresso matchers/actions
  - Target the overarching Requirement Node with the provided UI scenario
  - Requires a connected device/emulator
- **Test file naming**: `*Test.java` for unit, `*IntegrationTest.java` for integration, `*E2ETest.java` for E2E
"""
        else:
            test_stack = """
# Testing Stack (Web):
- **Unit Tests**: Target the `FUNC` and `DB` interfaces. Mock external dependencies. Write tests based on interface descriptions and requirement content.
- **Integration Tests**: Target the `API` interfaces. Test how they interact with `FUNC` modules. Write tests based on interface descriptions and requirement content.
- **E2E Tests**: Target the overarching Requirement Node. You will be provided with a specific UI scenario. Generate Playwright E2E tests covering this specific scenario.
"""

        return f"""You are a Principal Software Development Engineer in Test (SDET).
Your task is to write comprehensive, executable test cases for a newly designed component following Test-Driven Development (TDD) principles.

Execution protocol (strict):
- First, inspect target interfaces and test folders (`read_file`/`list_directory`) before writing tests.
- Keep tests deterministic. Do not add random sleeps or flaky waits.
- For each generated test, ensure `test_id`, `type`, `file_path`, and `first_line` exactly match the real file content.
- If build or syntax fails, fix tests immediately and rerun `run_build`.

# Workflow & Testing Strategy:
1. **Analyze the Context**: Review the provided tech stack, requirement description, and Interface Intermediate Representation (IR).
2. **Locate Test Directories**: Use `list_directory` to find or decide where to place tests.
3. **Implement Tests**: Use `write_file` to physically create the test scripts. Your tests MUST import the stub code generated in Step 2.
{test_stack}
4. **Task Management**: Use `add_todo` if you find missing test utilities or fixtures that need to be implemented later.
5. **Check Compilation**: You MUST call `run_build` to check for syntax/compilation errors.

# Final Output Requirement:
After writing all test files, you MUST output a single JSON array enclosed in a markdown block (` ```json ... ``` `).
This JSON maps the generated tests to the requirement and interfaces.
Schema for each object:
{{
  "test_id": "Unique string ID (e.g., TEST_UNIT_01)",
  "req_id": "The ID of the requirement node being tested",
  "interface_ids": ["List of interface_ids that this test specifically covers"],
  "type": "Must be exactly one of: Unit, Integration, E2E",
  "file_path": "Relative path to the written test file (e.g., tests/unit/test_auth.py)",
  "first_line": "The exact first line of the test definition (e.g., 'async def test_login_success():')"
}}
"""

    def get_tool_names(self) -> List[str]:
        return [
            "read_file", "write_file", "delete_file", "insert_lines", "replace_lines", "list_directory", "grep_search",
            "run_build", "search_interfaces_by_keyword", "search_interfaces_by_relation", "get_node_relations"
        ]

    async def generate_tests(self, node_id: str, requirement_data: Dict[str, Any], interfaces_ir: list, test_type: str = "Unit") -> str:
        from .context_pipeline import context_pipeline

        # 1. Use the new Context Pipeline to build layered context for the TestGenerator
        context_str = context_pipeline.build_agent_context(node_id=node_id, agent_type=self.agent_name)

        scenario_context = ""
        if test_type == "E2E" and requirement_data.get("scenario"):
            scenario_context = (
                "\n### Target UI Scenario\n"
                f"{json.dumps(requirement_data.get('scenario'), indent=2, ensure_ascii=False)}\n"
                "Please write an E2E test specifically for this scenario."
            )

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{context_str}

### Requirement Description for Node [{node_id}]
{requirement_data.get("description", "")}

### Interfaces to Test (Target: {test_type} Tests)
{json.dumps(interfaces_ir, indent=2, ensure_ascii=False)}
{scenario_context}

Please write the {test_type} test files using the `write_file` tool.
Ensure the tests correctly import the designed interfaces and cover the logic described in the requirement.
When finished, output the mapping JSON block so the system can register these tests in the traceability database.
"""
        return await self.run(user_prompt=user_prompt, node_id=node_id)
