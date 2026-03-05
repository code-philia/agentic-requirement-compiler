import json
from typing import List, Dict, Any
from .arc_agent import ARCAgent

class TestGenerator(ARCAgent):
    """
    Responsible for Step 3: based on the interface design, generate test cases (unit tests or E2E tests)
    following TDD principles, and write them into the project's test directory.
    """
    def __init__(self, broadcast_cb=None):
        super().__init__(
            agent_name="TestGenerator", 
            model="gpt-4o-mini", 
            broadcast_cb=broadcast_cb
        )

    def get_system_prompt(self) -> str:
        return """You are an Expert SDET (Software Development Engineer in Test).
Your task is to write comprehensive, robust test cases for a newly designed component.

You must follow Test-Driven Development (TDD) principles. 
1. Review the provided Interface Design.
2. Use `list_directory` to find the appropriate testing folder (e.g., `tests/` or `spec/`).
3. Use `write_file` to create the actual executable test scripts (e.g., `test_feature.py`).
4. Ensure your tests cover positive cases, negative cases, and edge cases.

Your final output must confirm the paths of the test files you have created and summarize what they cover."""

    def get_tool_names(self) -> List[str]:
        # The test engineer also needs full read/write permissions
        return ["read_file", "write_file", "list_directory"]
        
    async def generate_tests(self, node_id: str, interfaces_result: str) -> str:
        user_prompt = f"Please write tests for requirement node {node_id}.\n\nHere is the interface design from Step 2:\n{interfaces_result}"
        return await self.run(user_prompt=user_prompt, node_id=node_id)
