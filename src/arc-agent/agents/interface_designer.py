import json
from typing import List, Dict, Any
from .arc_agent import ARCAgent

class InterfaceDesigner(ARCAgent):
    def __init__(self, broadcast_cb=None):
        super().__init__(
            agent_name="InterfaceDesigner", 
            broadcast_cb=broadcast_cb
        )

    def get_system_prompt(self) -> str:
        return """You are a Principal Software Architect and Engineer.
Your task is to analyze a raw software requirement, design its interfaces (UI -> API -> FUNC -> DB), and implement them as concrete, executable STUB CODE in the real project directory.

# Workflow:
1. **Analyze and Design (Top-Down)**: 
   - Understand the current requirement and how it fits into the provided dependencies/context.
   - Decompose the requirement into: UI (if applicable), API, FUNC (Core Logic), and DB (Storage).
   - **REUSE FIRST**: Before designing a new interface, proactively explore the database to find existing ones. 
     - Use `search_interfaces_by_keyword` to find logic by name (e.g., 'auth', 'payment').
     - Use `search_interfaces_by_relation` to find interfaces from parent/child/sibling nodes that you might need to integrate with.
2. **Interface Reuse Mechanism**:
   - If an existing interface perfectly matches your needs, mark it for reuse in your final JSON output by setting `"reuse": true` and providing its exact existing `"interface_id"`. You don't need to rewrite its stub code unless modifying it.
   - If an existing interface needs slight modification to support your new requirement, you MUST first call `find_interface_impacts` to see what other interfaces call it. Then modify the file using `replace_lines`, ensuring you don't break existing callers (e.g., by adding optional parameters).
3. **Generate/Modify Stub Code**: 
   - Use `write_file` to create new files or `replace_lines` to update reused files.
   - The code MUST be syntactically valid.
   - Define exact inputs (arguments/types) and outputs (return types).
   - Define the calling relationships: If Interface A calls Interface B, Interface A's stub must import and call B.
   - Leave the actual business logic unimplemented (e.g., use `pass`, `raise NotImplementedError`, or return mock data).
4. **Check Compilation**: After implementing the interfaces, you MUST call the `run_build` tool to check for compilation errors. Use the build results log to fix any compilation or syntax errors before proceeding.

# Final Output Requirement:
After you have designed the architecture and written all the files, you MUST output a single JSON array in a markdown block (` ```json ... ``` `).
This JSON represents the Intermediate Representation (IR) mapping of the interfaces you just designed, implemented, or reused.
Each object in the array must follow this exact schema:
{
  "interface_id": "Unique string ID (if reusing, MUST use the exact existing ID)",
  "reuse": true or false,
  "type": "Must be exactly one of: UI, API, FUNC, DB",
  "name": "Logical name of the module/function",
  "description": "Brief description of its purpose",
  "inputs": ["List of input parameter descriptions or types"],
  "outputs": ["List of output data descriptions or types"],
  "callers": ["List of interface_ids that call this module"],
  "callees": ["List of interface_ids that this module calls"],
  "file_path": "The relative path to the file (e.g., src/api/user.py)",
  "first_line": "The exact first line of the function/class definition (e.g., 'async def login_user(request: Request) -> Response:')"
}
"""

    def get_tool_names(self) -> List[str]:
        return [
            "read_file", "write_file", "delete_file", "insert_lines", "replace_lines", "list_directory", "grep_search", "run_build", "search_interfaces_by_keyword", "search_interfaces_by_relation", "find_interface_impacts", "get_node_relations"
        ]

    async def design(self, node_id: str, requirement_data: dict, tech_stack: str, dependency_context: str = "") -> str:
        user_prompt = f"""
### Tech Stack Context
{tech_stack}

### Current Target Requirement Node (ID: {node_id})
{json.dumps(requirement_data, indent=2, ensure_ascii=False)}

{dependency_context}

Please perform the top-down decomposition for Node [{node_id}].
Then, generate the stub code files using the `write_file` tool. 
Ensure your stubs import and use any required dependency interfaces from the context above.
When finished, output the mapping JSON block so the system can update the traceability database.
"""
        return await self.run(user_prompt=user_prompt, node_id=node_id, max_steps=15)
