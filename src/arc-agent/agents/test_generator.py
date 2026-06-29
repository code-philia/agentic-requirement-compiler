import json
from typing import List, Dict, Any
from .arc_agent import ARCAgent

class TestGenerator(ARCAgent):
    def __init__(self, log_cb=None):
        super().__init__(
            agent_name="TestGenerator",
            log_cb=log_cb
        )

    def get_system_prompt(self) -> str:
        from utils import get_app_type, get_android_package, get_web_base_url, get_web_port
        app_type = get_app_type()

        if app_type == "android":
            android_pkg = get_android_package()
            pkg_dir = android_pkg.replace('.', '/')
            test_stack = f"""
# Testing Stack (Android):

## Test Directory Structure (MANDATORY)
Tests are organized into sub-packages by type. Each type has its own directory and package:
```
app/src/test/java/{pkg_dir}/
  unit/
    *Test.java             package {android_pkg}.unit;
  integration/
    *IntegrationTest.java  package {android_pkg}.integration;
  e2e/
    *E2ETest.java          package {android_pkg}.e2e;
```
Gradle filters by package prefix: `--tests "{android_pkg}.unit.*"`, `--tests "{android_pkg}.integration.*"`, `--tests "{android_pkg}.e2e.*"`

## JVM-ONLY
ALL tests run on the JVM via `./gradlew testDebugUnitTest`. There is NO device and NO emulator.

## Forbidden Android Test APIs
- Do not use `ApplicationProvider`
- Do not use `ActivityScenario`
- Do not use `AndroidJUnit4`
- Do not use Espresso
- Do not use `InstrumentationRegistry`
- Do not write to `app/src/androidTest/`
- Do not modify `app/build.gradle`

## Runner Selection
Use JUnit 5 for pure JVM tests with mocked collaborators.
Use JUnit 4 + Robolectric when the test needs Android framework behavior, Context, Activity, Room, or AndroidViewModel.

## Test Placement
- Unit tests: `app/src/test/java/{pkg_dir}/unit/`
- Integration tests: `app/src/test/java/{pkg_dir}/integration/`
- E2E tests: `app/src/test/java/{pkg_dir}/e2e/`
"""
            pkg_compliance = f"""
### Package Compliance (CRITICAL for Android)
- Main source code uses `package {android_pkg};`
- Unit tests use `package {android_pkg}.unit;`
- Integration tests use `package {android_pkg}.integration;`
- E2E tests use `package {android_pkg}.e2e;`
- The package declaration must match the directory.
- Do not use `com.example.template` or any other package name.
"""
        else:
            web_port = get_web_port()
            web_base_url = get_web_base_url()
            test_stack = """
# Testing Stack (Web):

## Test Framework Boundaries (MANDATORY)
- Unit and Integration tests use Vitest.
- E2E tests use Playwright.
- Never mix Vitest and Playwright APIs in the same file.

## Web Test Placement Rules (MANDATORY)
- Backend Vitest tests must live under `backend/tests/...`
- Frontend Vitest tests must live under `frontend/src/...`
- Frontend hook/component tests that use React Testing Library belong to frontend Vitest.
- E2E tests must target browser behavior with Playwright and must not be authored as frontend Vitest files.
- Playwright E2E spec files must be written under `backend/test-e2e/...`.
- Do not place Playwright spec files under `frontend/src/...`, project-root `e2e/...`, or any other directory outside `backend/test-e2e/...`.

## Web Test Content Rules (MANDATORY)
- For Vitest, generate ESM test files using `import { describe, it, expect, vi } from 'vitest'`.
- Do not generate CommonJS Vitest imports such as `require('vitest')`.
- Do not generate E2E files that import `vitest`, `@testing-library/react`, `@testing-library/user-event`, or React hook testing helpers.
- Playwright E2E must use Playwright APIs such as `test`, `expect`, and `page`.
- Do not rely on copying `node_modules`, patching package internals, or inventing compatibility shims to make generated tests run.
- Treat runner/framework mismatches as test-generation bugs to fix in the test files themselves.
"""
            test_stack += f"""

## Runtime And Port Rules (MANDATORY)
- The web app uses ONE backend port only: `{web_port}`.
- The Express backend serves the built frontend dist on the same origin.
- E2E tests must target `{web_base_url}`.
- Prefer `process.env.PLAYWRIGHT_BASE_URL` when authoring Playwright navigation code, with `{web_base_url}` as the expected value.
- Do not assume or hardcode a separate frontend dev-server port such as `5173` or `5174`.

"""
            pkg_compliance = ""

        return f"""You are a Principal Software Development Engineer in Test (SDET).
Your task is to write comprehensive, executable test cases for a newly designed component following Test-Driven Development (TDD) principles.

Generate tests bottom-up:
- Start with Unit tests for specific FUNC/DB interfaces.
- Then write Integration tests for interface boundaries and collaboration.
- Finish with E2E tests for the current requirement node and its UI scenarios.

Execution protocol (strict):
- Write ALL test files FIRST using multiple `write_file` calls, THEN call `run_build` ONCE.
- Do NOT interleave `read_file` and `write_file` while authoring tests.
- Keep tests deterministic. Do not add random sleeps or flaky waits.
- For each generated test, ensure `test_id`, `type`, `file_path`, and `first_line` exactly match the real file content.
- If build or syntax fails, fix tests immediately using `edit_file` and rerun `run_build`.
- If build or syntax fails because of framework mismatch, wrong directory placement, or wrong module system, rewrite the test file itself. Do not expect a later runtime patch to save it.
- Use the provided `<project_structure>` as the default source of truth for file and directory locations.
- Avoid exploratory `glob` or `list_directory` calls unless the required location is still unclear after reading `<project_structure>`.

{pkg_compliance}
{test_stack}

# Workflow
1. Analyze the tech stack, requirement description, and Interface IR.
2. Use `list_directory` if needed to confirm target test directories.
3. Write all required test files.
4. Call `run_build` to catch syntax or compilation problems.
5. Fix test-generation mistakes immediately, especially framework, directory, and module-system mismatches.

# Final Output Requirement
After writing all test files, output a single JSON array enclosed in a markdown block (` ```json ... ``` `).
This JSON maps the generated tests to the requirement and interfaces.
Schema for each object:
{{
  "test_id": "Unique string ID (e.g., TEST_UNIT_01)",
  "req_id": "The ID of the requirement node being tested",
  "interface_ids": ["List of interface_ids that this test specifically covers"],
  "type": "Must be exactly one of: Unit, Integration, E2E",
  "file_path": "Relative path to the written test file",
  "first_line": "The exact first line of the test definition"
}}
"""

    def get_tool_names(self) -> List[str]:
        return [
            "read_file", "write_file", "edit_file", "delete_file", "list_directory", "glob", "grep",
            "run_build", "search_interfaces_by_keyword", "search_interfaces_by_relation", "get_node_relations"
        ]

    def build_initial_messages(self, node_id: str, requirement_data: Dict[str, Any], interfaces_ir: list, test_type: str = "Unit", preloaded_source: str = None) -> tuple:
        """Build the [system, user] messages and tools list without calling run().
        Returns (messages, tools) so the caller can use run_from_messages() or continue a session.
        """
        from .context_pipeline import context_pipeline

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id, agent_type=self.agent_name, preloaded_source=preloaded_source
        )

        scenarios_context = ""
        if requirement_data.get("scenarios"):
            scenarios_context = (
                "\n### Target UI Scenarios\n"
                f"{json.dumps(requirement_data.get('scenarios'), indent=2, ensure_ascii=False)}\n"
            )

        if test_type == "All":
            test_instruction = """
Generate ALL test types in a single pass:
1. Unit tests for DB and FUNC interfaces
2. Integration tests for API interfaces
3. E2E tests for UI interfaces

Write ALL test files using `write_file` calls FIRST, then call `run_build` ONCE to verify compilation.
For web projects, ensure the generated Unit, Integration, and E2E files already match the correct framework and directory rules in one pass.
"""
        else:
            test_instruction = f"""
Please write the {test_type} test files using the `write_file` tool.
"""

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}

{scenarios_context}

{test_instruction}
Target the interfaces of the current node. Consider the node requirement content, each interface's responsibility, and its spec to decide what to test and how to assert.
When finished, output the mapping JSON block so the system can register these tests in the traceability database.
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

    async def generate_tests(self, node_id: str, requirement_data: Dict[str, Any], interfaces_ir: list, test_type: str = "Unit", preloaded_source: str = None) -> str:
        messages, tools = self.build_initial_messages(
            node_id=node_id, requirement_data=requirement_data,
            interfaces_ir=interfaces_ir, test_type=test_type,
            preloaded_source=preloaded_source
        )
        result, _ = await self.run_from_messages(messages, node_id=node_id, max_steps=15, tools=tools)
        return result
