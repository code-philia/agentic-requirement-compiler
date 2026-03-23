import json
from typing import List, Dict, Any
from .arc_agent import ARCAgent

class TestDrivenDeveloper(ARCAgent):
    def __init__(self, broadcast_cb=None):
        super().__init__(
            agent_name="TestDrivenDeveloper", 
            model="gpt-4o",
            broadcast_cb=broadcast_cb
        )

    def get_system_prompt(self) -> str:
        return """You are an Elite Full-Stack Developer strictly following Test-Driven Development (TDD).
Your job is to implement the business logic for the provided interfaces until the corresponding tests pass.

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

### Workflow:
1. Review the existing stub files and test files using `read_file`.
2. Run the tests using the `run_tests` tool to see the current failures (Red phase).
3. Use `write_file` to implement the actual logic in the corresponding layers (e.g., SQLite models, Express controllers, or React components) (Green phase).
4. Re-run `run_tests`. If it fails, read the output log, fix the code, and repeat.
5. If you need a new npm package, use `execute_command` (e.g., `npm install cors`).

Once `run_tests` returns a 100% passing state (Exit Code: 0) for the target tests, you MUST output exactly the word "IMPLEMENTED" in your final response to complete the task.
"""

    def get_tool_names(self) -> List[str]:
        return ["read_file", "write_file", "delete_file", "insert_lines", "replace_lines", "list_directory", "grep_search", "add_todo", "list_todos", "check_todo", "clear_todos", "execute_command", "run_tests", "run_build"]
        
    async def implement(self, node_id: str, test_files: List[str], test_type: str, req_desc: str, scenario: dict = None) -> str:
        scenario_context = ""
        if test_type == "E2E" and scenario:
            scenario_context = f"\n### Target UI Scenario\n{json.dumps(scenario, indent=2, ensure_ascii=False)}"

        user_prompt = f"""
### Implementation Task for Node [{node_id}]
Target Test Type: {test_type}

### Requirement Description
{req_desc}
{scenario_context}

### Target Test Files
{json.dumps(test_files, indent=2)}

Please start your TDD loop. 
Call `run_tests` passing the exact test type (`{test_type}`) and optionally the test files to run them.
Implement the code to make these specific tests pass.
Do not stop until the target tests pass. Reply with "IMPLEMENTED" when done.
"""
        return await self.run(user_prompt=user_prompt, node_id=node_id, max_steps=15)
