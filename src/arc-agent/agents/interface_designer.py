import json
from typing import List, Dict, Any
from .arc_agent import ARCAgent

class InterfaceDesigner(ARCAgent):
    def __init__(self, broadcast_cb=None):
        super().__init__(
            agent_name="InterfaceDesigner", 
            model="gpt-4o-mini", 
            broadcast_cb=broadcast_cb
        )

    def get_system_prompt(self) -> str:
        return """You are a Principal Software Engineer. 
Your task is to take a technology-agnostic Intermediate Representation (IR) of interfaces and implement them as concrete, executable STUB CODE in the real project directory.

# Workflow:
1. **Understand Tech Stack**: Review the provided technology stack and project structure (use `list_directory`, `grep_search`).
2. **Generate Stub Code**: For each interface in the IR, use `write_file` to create or update the corresponding file. 
- The code MUST be syntactically valid.
- Define exact inputs (arguments/types) and outputs (return types).
- Define the calling relationships: If Interface A calls Interface B, Interface A's stub must import and call B.
- Leave the actual business logic unimplemented (e.g., use `pass`, `raise NotImplementedError`, or return mock data).
3. **Manage Tasks**: If you notice dependencies that belong to other teams or future nodes, use `add_todo` to track them.

# Final Output Requirement:
After you have written all the files, you MUST output a single JSON array in a markdown block (` ```json ... ``` `) mapping each interface to its physical location. 
Schema for each object:
{
"interface_id": "The ID from the provided IR",
"file_path": "The relative path to the file you wrote (e.g., src/api/user.py)",
"first_line": "The exact first line of the function/class definition (e.g., 'async def login_user(request: Request) -> Response:')"
}
"""

    def get_tool_names(self) -> List[str]:
        return [
            "read_file", "write_file","delete_file", "insert_lines", "replace_lines", "list_directory", 
            "grep_search", 
            "add_todo", "list_todos", "check_todo", "clear_todos"
        ]


    async def design(self, node_id: str, interfaces_ir: list, tech_stack: str) -> str:
        user_prompt = f"""
### Tech Stack Context
{tech_stack}

### Interface IR to Implement for Node [{node_id}]
{json.dumps(interfaces_ir, indent=2)}

Please generate the stub code files using the `write_file` tool. 
When finished, output the mapping JSON block so the system can update the traceability database.
"""
        return await self.run(user_prompt=user_prompt, node_id=node_id)
