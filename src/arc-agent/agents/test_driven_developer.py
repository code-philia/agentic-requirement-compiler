import json
from typing import List, Dict, Any
from .arc_agent import ARCAgent

class TestDrivenDeveloper(ARCAgent):
    def __init__(self, broadcast_cb=None):
        super().__init__(
            agent_name="TestDrivenDeveloper",
            broadcast_cb=broadcast_cb
        )

    def get_system_prompt(self) -> str:
        from utils import get_app_type, get_android_package
        app_type = get_app_type()

        if app_type == "android":
            android_pkg = get_android_package()
            tech_stack = f"""
### Strict Tech Stack Constraints:
**Android:**
- Language: Java 8
- Build System: Gradle 8.4 + AGP 8.1.4
- UI: XML Layout + AndroidX AppCompat + Material Components + ConstraintLayout
- Database: Room (SQLite)
- Testing:
  - All tests run on JVM via `./gradlew testDebugUnitTest` (no device/emulator)
  - Unit: JUnit5 + Robolectric (app/src/test/)
  - Integration: JUnit5 + Robolectric + MockWebServer + Room in-memory DB (app/src/test/)
  - E2E: JUnit5 + Robolectric + ActivityScenario (app/src/test/)
  - **NEVER use `@RunWith(RobolectricTestRunner.class)`** — use `@Config(sdk = 31)` instead
  - **NEVER use `@RunWith(AndroidJUnit4.class)` in JVM tests** — only JUnit5 annotations
  - The `android-junit5` Gradle plugin is already configured — do NOT modify build.gradle to add/remove it
- Source directories: app/src/main/java/, app/src/test/java/
- Package: {android_pkg}
"""
        else:
            tech_stack = """
### Strict Tech Stack Constraints:
**Frontend:**
- Framework: React 18+ (Vite)
- Language: JavaScript (ES6+)
- Styling: Tailwind CSS v4
- HTTP: Axios (MUST use Interceptors for global error handling in `src/api/axios.js`).

**Backend:**
- Runtime: Node.js (LTS)
- Framework: Express.js
- Database: SQLite3 (`sqlite3` driver, file-based)
"""

        return f"""You are an Elite Full-Stack Developer strictly following Test-Driven Development (TDD).
Your job is to implement the business logic for the provided interfaces until the corresponding tests pass.

Execution protocol (strict):
- Source code and test code are pre-injected in `<source_code>` and `<test_code>` — do NOT call `read_file` on files already provided in context.
- Always start with `run_tests` for the requested scope and use failing output as the single source of truth.
- Fix the minimal set of files needed per iteration. Avoid broad refactors.
- After each fix, rerun `run_tests` for the same scope.
- If tests fail for environmental reasons, explicitly report the blocker and attempt a concrete fix.
- Return exactly "IMPLEMENTED" only when target tests are truly passing.

{tech_stack}

### Package Compliance (CRITICAL for Android):
- The application package is `{android_pkg}`. You MUST use this package for ALL generated code:
  - `package {android_pkg};` in every Java file
  - `import {android_pkg}.xxx;` for cross-module references
  - File paths must use `{android_pkg.replace('.', '/')}/` as the package directory
  - AndroidManifest.xml must reference activities as `{android_pkg}.ActivityName`
- Do NOT use `com.example.template` or any other package name.
- If the requirement description mentions a different package name in resource-id patterns, use THAT package name instead.

### Workflow:
1. Run the tests using the `run_tests` tool to see the current failures (Red phase).
2. Use `write_file` to implement the actual logic in the corresponding layers (Green phase).
3. Re-run `run_tests`. If it fails, read the output log, fix the code, and repeat.
4. If you need a new npm package, use `execute_command` (e.g., `npm install cors`).

Once `run_tests` returns a 100% passing state (Exit Code: 0) for the target tests, you MUST output exactly the word "IMPLEMENTED" in your final response to complete the task.
"""

    def get_tool_names(self) -> List[str]:
        return ["read_file", "write_file", "delete_file", "insert_lines", "replace_lines", "list_directory", "grep_search",
                "execute_command", "run_tests", "run_build", "search_interfaces_by_keyword", "search_interfaces_by_relation", "get_node_relations"]

    async def implement(self, node_id: str, test_files: List[str], test_type: str, req_desc: str, scenario: list = None, dependency_context: str = "", current_interfaces: list = None) -> str:
        from .context_pipeline import context_pipeline

        # 1. Use the new Context Pipeline to build layered context for the TDD Agent
        context_str = context_pipeline.build_agent_context(node_id=node_id, agent_type=self.agent_name)

        scenario_context = ""
        if test_type == "E2E" and scenario:
            scenario_context = f"\n### Target UI Scenario\n{json.dumps(scenario, indent=2, ensure_ascii=False)}"

        current_interfaces_str = "### Current Interfaces to Implement\n"
        if current_interfaces:
            for iface in current_interfaces:
                current_interfaces_str += f"- ID: {iface.get('interface_id')} (Type: {iface.get('type')})\n"
                if iface.get('file_path'):
                    current_interfaces_str += f"  File: `{iface.get('file_path')}`\n"
                if iface.get('first_line'):
                    current_interfaces_str += f"  Signature: `{iface.get('first_line')}`\n"
        else:
            current_interfaces_str += "No specific interface data provided."

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{context_str}

### Implementation Task for Node [{node_id}]
Target Test Type: {test_type}

### Requirement Description
{req_desc}
{scenario_context}

### Dependency Context
{dependency_context}

{current_interfaces_str}

### Target Test Files
{json.dumps(test_files, indent=2)}

Please start your TDD loop.
Call `run_tests` passing the exact test type (`{test_type}`) and optionally the test files to run them.
Implement the code to make these specific tests pass.
Do not stop until the target tests pass. Reply with "IMPLEMENTED" when done.
"""
        return await self.run(user_prompt=user_prompt, node_id=node_id, max_steps=15)
