import json
from typing import List, Dict, Any
from .arc_agent import ARCAgent

class InterfaceDesigner(ARCAgent):
    """
    Responsible for Step 2: based on the requirement analysis result, design concrete interfaces
    (APIs, classes, database tables, etc.), and optionally write them to files.
    """
    def __init__(self, broadcast_cb=None):
        super().__init__(
            agent_name="InterfaceDesigner", 
            model="gpt-4o-mini", 
            broadcast_cb=broadcast_cb
        )

    def get_system_prompt(self) -> str:
        return """You are a Principal Software Engineer. 
Your task is to take a technical specification (from the requirement analysis) and design the concrete interfaces for it.

Your design should include:
1. Data Models / Schemas: What data structures are needed?
2. API Endpoints or Function Signatures: Define the inputs and outputs.
3. Component Interactions: How will this node interact with existing parts of the system?

You should use `list_directory` and `read_file` to inspect the current project architecture to ensure your design is consistent with the existing codebase. 
If appropriate, use `write_file` to save your design document into a `docs/designs/` directory.

Once you have finalized the design, output a comprehensive summary of the designed interfaces."""

    def get_tool_names(self) -> List[str]:
        # The designer needs permission to read/write files and inspect the directory structure
        return ["read_file", "write_file", "list_directory"]
        
    async def design(self, node_id: str, analysis_result: str) -> str:
        user_prompt = f"Please design the interfaces for requirement node {node_id}.\n\nHere is the analysis result from Step 1:\n{analysis_result}"
        return await self.run(user_prompt=user_prompt, node_id=node_id)
