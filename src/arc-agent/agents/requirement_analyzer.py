import json
from typing import List, Dict, Any
from .arc_agent import ARCAgent

class RequirementAnalyzer(ARCAgent):
    def __init__(self, broadcast_cb=None):
        super().__init__(
            agent_name="RequirementAnalyzer", 
            model="gpt-4o-mini", 
            broadcast_cb=broadcast_cb
        )

    def get_system_prompt(self) -> str:
        return """You are a senior software architect and requirements analyst.
Your task is to analyze a raw software requirement node and output a detailed technical specification.

You should structure your response with the following sections:
1. Core Objective: What needs to be achieved.
2. Technical Constraints: Any frameworks, languages, or specific rules to follow.
3. Edge Cases: Potential pitfalls to watch out for.

If you need more context about the project, you can use the `read_file` tool to inspect existing architecture documents or related source code.
"""

    def get_tool_names(self) -> List[str]:
        return ["read_file"]
        
    async def analyze(self, node_id: str, requirement_data: dict) -> str:
        user_prompt = f"Please analyze the following requirement node (ID: {node_id}):\n\n{json.dumps(requirement_data, indent=2)}"
        return await self.run(user_prompt=user_prompt, node_id=node_id)