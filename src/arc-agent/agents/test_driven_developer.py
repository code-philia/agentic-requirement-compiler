import json
from typing import List, Dict, Any
from .arc_agent import ARCAgent

class TestDrivenDeveloper(ARCAgent):
    """
    Responsible for Step 4: implement concrete business logic based on the interface design
    and test cases. In the TDD loop, if tests fail, it keeps self-correcting based on
    the error logs.
    """
    def __init__(self, broadcast_cb=None):
        super().__init__(
            agent_name="TestDrivenDeveloper", 
            model="gpt-4o-mini",  # In a real coding phase, consider upgrading to gpt-4o or claude-3.5-sonnet
            broadcast_cb=broadcast_cb
        )

    def get_system_prompt(self) -> str:
        return """You are a Senior Software Engineer executing Test-Driven Development (TDD).
Your task is to implement the business logic to fulfill a requirement node and make sure all existing tests pass.

Workflow:
1. Use `read_file` or `list_directory` to understand the existing tests and interface designs.
2. Use `write_file` to implement the required source code.
3. Use `run_tests` to execute the test suite against your code.
4. If tests fail, analyze the error output and repeat the fix process.

When the `run_tests` tool indicates that all tests have passed successfully, output the exact word 'IMPLEMENTED' to signify completion.
"""

    def get_tool_names(self) -> List[str]:
        # The code generator has the highest privileges, including executing test commands
        return ["read_file", "write_file", "list_directory", "run_tests"]
        
    async def implement(self, node_id: str, tests_summary: str, iteration: int) -> str:
        """Execute code generation; `iteration` indicates which TDD attempt this is"""
        user_prompt = f"""Please implement the logic for requirement node {node_id}.
This is TDD Iteration {iteration}. 
Here is the summary of the generated tests from Step 3:
{tests_summary}

Remember to use the `run_tests` tool to verify your work!"""
        
        return await self.run(user_prompt=user_prompt, node_id=node_id)
