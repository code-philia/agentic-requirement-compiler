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
        from utils import get_app_type
        app_type = get_app_type()

        if app_type == "android":
            tech_stack = """
### Strict Tech Stack Constraints:
**Android:**
- Language: Java 8
- Build System: Gradle 7.2 + AGP 7.1.2
- UI: XML Layout + AndroidX AppCompat + Material Components + ConstraintLayout
- Database: Room (SQLite)
- Testing:
  - Unit: JUnit5 + Robolectric (app/src/test/)
  - Integration: Robolectric + MockWebServer + Room in-memory DB (app/src/test/)
  - E2E: Espresso (app/src/androidTest/) - requires connected device/emulator
- Source directories: app/src/main/java/, app/src/test/java/, app/src/androidTest/java/
- Package: com.example.template
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
- Always start with `run_tests` for the requested scope and use failing output as the single source of truth.
- Fix the minimal set of files needed per iteration. Avoid broad refactors.
- After each fix, rerun `run_tests` for the same scope.
- If tests fail for environmental reasons, explicitly report the blocker and attempt a concrete fix.
- Return exactly "IMPLEMENTED" only when target tests are truly passing.

{tech_stack}

### Workflow:
1. Review the existing stub files and test files using `read_file`.
2. Run the tests using the `run_tests` tool to see the current failures (Red phase).
3. Use `write_file` to implement the actual logic in the corresponding layers (Green phase).
4. Re-run `run_tests`. If it fails, read the output log, fix the code, and repeat.
5. If you need a new npm package, use `execute_command` (e.g., `npm install cors`).

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
