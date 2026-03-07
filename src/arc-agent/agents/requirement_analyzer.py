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
        return """You are a Principal Software Architect and Systems Analyst.
Your task is to analyze a raw software requirement and perform a strict TOP-DOWN architectural decomposition (UI -> API -> FUNC -> DB).

# Workflow & Decomposition Rules:
1. **Understand Context**: Analyze the overall project architecture and how this specific node fits into the global goal.
2. **Top-Down Design**: 
- UI (User Interface): Design what the user interacts with (or skip if this is a purely backend requirement).
- API (Application Programming Interface): Design the network/communication layer that the UI calls.
- FUNC (Core Logic/Functions): Design the internal service/module functions called by the API.
- DB (Database/Storage): Design the data models, tables, or queries utilized by the FUNC layer.
3. **Multiplicity**: You can generate multiple modules for any layer (e.g., one UI might call two APIs, which rely on three FUNCs).

# Output Structure:
You MUST structure your response strictly into two parts:

### Part 1: Architectural Analysis (Natural Language)
Provide a clear, natural language explanation of the decomposition. For each module (UI/API/FUNC/DB), describe its core functionality, inputs, and outputs. Explain how data flows from top to bottom.

### Part 2: Intermediate Representation (IR)
You MUST output a single JSON array enclosed in a markdown json block (` ```json ... ``` `). This JSON is a technology-agnostic Intermediate Representation of the interfaces.
Each object in the array must follow this exact schema:
{
"interface_id": "Unique string ID (e.g., REQID_TYPE_NUM)",
"type": "Must be exactly one of: UI, API, FUNC, DB",
"name": "Logical name of the module",
"description": "Brief description of its purpose",
"inputs": ["List of input parameter descriptions or types"],
"outputs": ["List of output data descriptions or types"],
"callers": ["List of interface_ids that call this module"],
"callees": ["List of interface_ids that this module calls"]
}
"""

    def get_tool_names(self) -> List[str]:
        return ["read_file", "list_directory"]
        
    async def analyze(self, node_id: str, requirement_data: dict, project_context: str = "", global_map: str = "") -> str:        
        user_prompt = f"""
### 1. Global Project Context
{project_context if project_context else "No global context provided."}

### 2. Global Requirements Map (DAG)
{global_map if global_map else "No global map provided."}

### 3. Current Target Requirement Node (ID: {node_id})
{json.dumps(requirement_data, indent=2)}

Please perform the top-down decomposition for Node [{node_id}] and output the Natural Language Analysis followed by the JSON IR.
"""
        return await self.run(user_prompt=user_prompt, node_id=node_id)
    


def parse_and_store_interfaces(llm_output: str, req_id: str) -> List[Dict]:
    """
    Parse the JSON array from LLM output and store each interface in the database.
    """
    match = re.search(r'```json\s*(.*?)\s*```', llm_output, re.DOTALL | re.IGNORECASE)
    
    if not match:
        print(f"[Warning] No JSON block found in output for Node {req_id}")
        return []
        
    json_str = match.group(1)
    
    try:
        interfaces = json.loads(json_str)
        if not isinstance(interfaces, list):
            print(f"[Warning] JSON output for Node {req_id} is not a list.")
            return []
            
        for iface in interfaces:
            interface_id = iface.get("interface_id", f"{req_id}_UNKNOWN")
            itype = iface.get("type", "FUNC")
            callers = iface.get("callers", [])
            callees = iface.get("callees", [])
            
            content_dict = {
                "name": iface.get("name", ""),
                "description": iface.get("description", ""),
                "inputs": iface.get("inputs", []),
                "outputs": iface.get("outputs", [])
            }
            content_str = json.dumps(content_dict, ensure_ascii=False)
            
            insert_interface(
                interface_id=interface_id,
                req_id=req_id,
                type=itype,
                content=content_str,
                file_path="",       
                first_line="",      
                implemented=False,  
                callers=callers,
                callees=callees
            )
            
        return interfaces
        
    except json.JSONDecodeError as e:
        print(f"[Error] Failed to parse JSON for Node {req_id}: {str(e)}")
        return []
