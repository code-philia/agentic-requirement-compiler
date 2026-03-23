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
        return """You are a Principal Software Development Engineer in Test (SDET).
Your task is to write comprehensive, executable test cases for a newly designed component following Test-Driven Development (TDD) principles.

# Workflow & Testing Strategy:
1. **Analyze the Context**: Review the provided tech stack, requirement description, and Interface Intermediate Representation (IR).
2. **Locate Test Directories**: Use `list_directory` to find or decide where to place tests (e.g., `tests/unit/`, `tests/integration/`, `backend/test-e2e/`).
3. **Implement Tests**: Use `write_file` to physically create the test scripts. Your tests MUST import the stub code generated in Step 2.
   - **Unit Tests**: Target the `FUNC` and `DB` interfaces. Mock external dependencies. Write tests based on interface descriptions and requirement content.
   - **Integration Tests**: Target the `API` interfaces. Test how they interact with `FUNC` modules. Write tests based on interface descriptions and requirement content.
   - **E2E Tests**: Target the overarching Requirement Node. You will be provided with a specific UI scenario. Generate Playwright E2E tests covering this specific scenario.
4. **Task Management**: Use `add_todo` if you find missing test utilities or fixtures that need to be implemented later.
5. **Check Compilation**: You MUST call `run_build` to check for syntax/compilation errors.

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
            "add_todo", "list_todos", "check_todo", "clear_todos", "run_build", "retrieve_context", "get_node_relations"
        ]

    async def generate_tests(self, node_id: str, interfaces_ir: list, tech_stack: str, test_type: str = "Unit", req_desc: str = "", scenario: dict = None, dependency_context: str = "") -> str:
        scenario_context = ""
        if test_type == "E2E" and scenario:
            scenario_context = f"\n### Target UI Scenario\n{json.dumps(scenario, indent=2, ensure_ascii=False)}\nPlease write a Playwright E2E test specifically for this scenario."

        user_prompt = f"""
### Tech Stack Context
{tech_stack}

{dependency_context}

### Requirement Description for Node [{node_id}]
{req_desc}

### Interfaces to Test (Target: {test_type} Tests)
{json.dumps(interfaces_ir, indent=2, ensure_ascii=False)}
{scenario_context}

Please write the {test_type} test files using the `write_file` tool. 
Ensure the tests correctly import the designed interfaces and cover the logic described in the requirement.
When finished, output the mapping JSON block so the system can register these tests in the traceability database.
"""
        return await self.run(user_prompt=user_prompt, node_id=node_id)
