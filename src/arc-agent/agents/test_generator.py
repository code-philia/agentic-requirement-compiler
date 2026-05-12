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
        from utils import get_app_type, get_android_package
        app_type = get_app_type()

        if app_type == "android":
            android_pkg = get_android_package()
            pkg_dir = android_pkg.replace('.', '/')
            test_stack = f"""
# Testing Stack (Android):
- **Unit Tests**: JUnit5 + Robolectric — place in `app/src/test/java/{pkg_dir}/`
  - Use `@Test` from `org.junit.jupiter.api.Test`
  - Use `@org.robolectric.annotation.Config(sdk = 31)` to configure Robolectric SDK
  - NEVER use `@RunWith(RobolectricTestRunner.class)` — it conflicts with JUnit5
  - Use `@BeforeEach` / `@AfterEach` (JUnit5) for setup/teardown
  - Use `@DisplayName` for readable test names
  - Use `@Nested` for grouping related tests
  - Target `FUNC` and `DB` interfaces
- **Integration Tests**: JUnit5 + Robolectric + MockWebServer + Room in-memory DB
  - Place in `app/src/test/java/{pkg_dir}/`
  - Use `Room.inMemoryDatabaseBuilder(context, AppDatabase.class).allowMainThreadQueries().build()` for real DB
  - Use `MockWebServer` for HTTP testing (enqueue fake responses)
  - Use `@AfterEach` to close DB and shutdown MockWebServer
  - Target `API` interfaces
- **E2E Tests**: JUnit5 + Robolectric — place in `app/src/test/java/{pkg_dir}/`
  - Use `@Config(sdk = 31)` to simulate Activity lifecycle
  - Use `ActivityScenario` from `androidx.test.core` to launch Activities
  - Use MockWebServer to mock backend API responses
  - Target the overarching Requirement Node with the provided UI scenario
- **Test file naming**: `*Test.java` for unit, `*IntegrationTest.java` for integration, `*E2ETest.java` for E2E
- **CRITICAL**: Do NOT use `@RunWith` annotations. JUnit5 test discovery is handled by the `android-junit5` Gradle plugin (already configured in build.gradle).
- **Package**: {android_pkg}
"""
            pkg_compliance = f"""
### Package Compliance (CRITICAL for Android):
- The application package is `{android_pkg}`. You MUST use this package for ALL test code:
  - `package {android_pkg};` in every test Java file
  - `import {android_pkg}.xxx;` to import the classes under test
  - Place test files under `app/src/test/java/{pkg_dir}/`
- Do NOT use `com.example.template` or any other package name.
"""
        else:
            test_stack = """
# Testing Stack (Web):
- **Unit Tests**: Target the `FUNC` and `DB` interfaces. Mock external dependencies. Write tests based on interface descriptions and requirement content.
- **Integration Tests**: Target the `API` interfaces. Test how they interact with `FUNC` modules. Write tests based on interface descriptions and requirement content.
- **E2E Tests**: Target the overarching Requirement Node. You will be provided with a specific UI scenario. Generate Playwright E2E tests covering this specific scenario.
"""
            pkg_compliance = ""

        return f"""You are a Principal Software Development Engineer in Test (SDET).
Your task is to write comprehensive, executable test cases for a newly designed component following Test-Driven Development (TDD) principles.

Execution protocol (strict):
- Source code is pre-injected in `<source_code>` — do NOT call `read_file` on source files already provided in context.
- Write ALL test files FIRST using multiple `write_file` calls, THEN call `run_build` ONCE.
- Do NOT interleave `read_file` and `write_file` — batch all writes together.
- Keep tests deterministic. Do not add random sleeps or flaky waits.
- For each generated test, ensure `test_id`, `type`, `file_path`, and `first_line` exactly match the real file content.
- If build or syntax fails, fix tests immediately and rerun `run_build`.

{pkg_compliance}
# Workflow & Testing Strategy:
1. **Analyze the Context**: Review the provided tech stack, requirement description, and Interface Intermediate Representation (IR).
2. **Locate Test Directories**: Use `list_directory` to find or decide where to place tests.
3. **Implement Tests**: Use `write_file` to physically create the test scripts. Your tests MUST import the stub code generated in Step 2.
{test_stack}
4. **Check Compilation**: You MUST call `run_build` to check for syntax/compilation errors.

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
        if requirement_data.get("scenario"):
            scenario_context = (
                "\n### Target UI Scenario\n"
                f"{json.dumps(requirement_data.get('scenario'), indent=2, ensure_ascii=False)}\n"
            )

        if test_type == "All":
            test_instruction = """
Generate ALL test types in a single pass:
1. **Unit Tests** for DB and FUNC interfaces
2. **Integration Tests** for API interfaces
3. **E2E Tests** for UI interfaces (use the scenario above if provided)

Write ALL test files using `write_file` calls FIRST, then call `run_build` ONCE to verify compilation.
Do NOT call `read_file` on source files — they are already provided in the `<source_code>` context above.
"""
        else:
            test_instruction = f"""
Please write the {test_type} test files using the `write_file` tool.
Do NOT call `read_file` on source files — they are already provided in the `<source_code>` context above.
"""

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{context_str}

### Requirement Description for Node [{node_id}]
{requirement_data.get("description", "")}

### Interfaces to Test
{json.dumps(interfaces_ir, indent=2, ensure_ascii=False)}
{scenario_context}

{test_instruction}
Ensure the tests correctly import the designed interfaces and cover the logic described in the requirement.
When finished, output the mapping JSON block so the system can register these tests in the traceability database.
"""
        return await self.run(user_prompt=user_prompt, node_id=node_id, max_steps=15)
