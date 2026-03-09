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
Your job is to implement the business logic for the provided interfaces until all corresponding tests pass.

### Strict Tech Stack Constraints:
**Frontend:**
- Framework: React 18+ (Vite)
- Language: JavaScript (ES6+)
- Styling: Tailwind CSS v4
- HTTP: Axios (MUST use Interceptors for global error handling in `src/api/axios.js`).
- Testing: NONE in frontend. Rely entirely on backend E2E.

**Backend:**
- Runtime: Node.js (LTS)
- Framework: Express.js
- Database: SQLite3 (`sqlite3` driver, file-based)
- Testing: Vitest (Unit/Integration) and Playwright (E2E in `backend/test-e2e/`).

### Workflow:
1. Review the existing stub files and tests using `read_file`.
2. Run the tests using the `run_tests` tool to see the current failures (Red phase).
3. Use `write_file` to implement the actual logic in the Express controllers, SQLite models, or React components (Green phase).
4. Re-run `run_tests`. If it fails, read the stderr, fix the code, and repeat.
5. If you need a new npm package, use `execute_command` (e.g., `npm install cors`).

Once `run_tests` returns a 100% passing state (Exit Code: 0) for the target requirement, you MUST output exactly the word "IMPLEMENTED" in your final response to complete the task.
"""

    def get_tool_names(self) -> List[str]:
        return ["read_file", "write_file", "list_directory", "grep_search", "add_todo", "list_todos", "check_todo", "clear_todos", "execute_command", "run_tests"]
        
    async def implement(self, node_id: str, tests_summary: str, iteration: int) -> str:
        user_prompt = f"""
### Implementation Task for Node [{node_id}]
This is TDD Iteration {iteration}.

Here is the summary of tests generated in Step 3:
{tests_summary}

Please start your TDD loop. Run the tests, implement the code, and repeat. 
Do not stop until all tests pass. Reply with "IMPLEMENTED" when done.
"""
        return await self.run(user_prompt=user_prompt, node_id=node_id, max_steps=15)
