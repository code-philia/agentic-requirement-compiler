import json
from typing import List, Dict, Any
from .arc_agent import ARCAgent

class TestGenerator(ARCAgent):
    def __init__(self, broadcast_cb=None):
        super().__init__(
            agent_name="TestGenerator", 
            model="gpt-4o",
            broadcast_cb=broadcast_cb
        )

    def get_system_prompt(self) -> str:
        return """You are a Principal Software Development Engineer in Test (SDET).
Your task is to write comprehensive, executable test cases for a newly designed component following Test-Driven Development (TDD) principles.

# Workflow & Testing Strategy:
1. **Analyze the Interfaces**: Review the provided tech stack and Interface Intermediate Representation (IR).
2. **Locate Test Directories**: Use `list_directory` to find or decide where to place tests (e.g., `tests/unit/`, `tests/integration/`, `tests/e2e/`).
3. **Implement Tests**: Use `write_file` to physically create the test scripts. Your tests MUST import the stub code generated in Step 2.
   - **Unit Tests**: Target the `FUNC` and `DB` interfaces. Mock external dependencies.
   - **Integration Tests**: Target the `API` interfaces. Test how they interact with `FUNC` modules.
   - **E2E Tests**: Target the overarching Requirement Node. Simulate a full user flow (could be testing the `UI` or hitting the API endpoints sequentially).
4. **Task Management**: Use `add_todo` if you find missing test utilities or fixtures that need to be implemented later.

# Final Output Requirement:
After writing all test files, you MUST output a single JSON array enclosed in a markdown block (` ```json ... ``` `). 
This JSON maps the generated tests to the requirement and interfaces.
Schema for each object:
{
  "test_id": "Unique string ID (e.g., TEST_UNIT_01)",
  "req_id": "The ID of the requirement node being tested",
  "interface_ids": ["List of interface_ids that this test specifically covers"],
  "type": "Must be exactly one of: Unit, Integration, E2E",
  "file_path": "Relative path to the written test file (e.g., tests/unit/test_auth.py)",
  "first_line": "The exact first line of the test definition (e.g., 'async def test_login_success():')"
}
"""

    def get_tool_names(self) -> List[str]:
        return [
            "read_file", "write_file", "delete_file", "insert_lines", "replace_lines", "list_directory", "grep_search", 
            "add_todo", "list_todos", "check_todo", "clear_todos", "run_build"
        ]

    async def generate_tests(self, node_id: str, interfaces_ir: list, tech_stack: str) -> str:
        user_prompt = f"""
### Tech Stack Context
{tech_stack}

### Interfaces to Test for Node [{node_id}]
{json.dumps(interfaces_ir, indent=2)}

Please write the test files (Unit, Integration, E2E) using the `write_file` tool. 
Ensure the tests correctly import the designed interfaces.
When finished, output the mapping JSON block so the system can register these tests in the traceability database.
"""
        return await self.run(user_prompt=user_prompt, node_id=node_id)
