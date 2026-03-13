import json
import re
import base64
import mimetypes
import os
from typing import List, Dict, Any
from .arc_agent import ARCAgent
from ..traceability.database import insert_interface, update_requirement_visuals

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
    
    async def parse_and_store_visual_elements(self, workspace_path: str, requirement_data: dict) -> None:
        """
        Extract the image url from the description of the requirement.
        Parse the content using llm and store the result in the requirement table in the database.
        """
        description = requirement_data.get("description", "")
        req_id = requirement_data.get("id", "")
        if not description or not req_id:
            return

        # 1. Extract image paths
        # Format: [image]("./path/to/image")
        matches = re.findall(r'\[image\]\("([^"]+)"\)', description)
        if not matches:
            return

        visual_references = []

        for image_path in matches:
            normalized_path = os.path.normpath(image_path)
            
            # Strip leading separators to ensure it is treated as a relative path to workspace_path
            # This handles cases where markdown path starts with / or \
            if normalized_path.startswith(os.sep):
                normalized_path = normalized_path.lstrip(os.sep)
                
            full_path = os.path.join(workspace_path, normalized_path)
            full_path = os.path.abspath(full_path)
            
            if not os.path.exists(full_path):
                print(f"[Warning] Image not found: {full_path}")
                continue
                
            # Encode image
            try:
                mime_type, _ = mimetypes.guess_type(full_path)
                if not mime_type:
                    mime_type = "image/png" # Default
                    
                with open(full_path, "rb") as image_file:
                    base64_image = base64.b64encode(image_file.read()).decode('utf-8')
                    
                # Call LLM
                prompt = visual_analysis_prompt()
                
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ]
                
                await self._log(f"Analyzing visual element: {image_path}", node_id=req_id)
                
                response = await self.client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=messages,
                    max_tokens=2000
                )
                
                analysis = response.choices[0].message.content
                visual_references.append({
                    "image_path": image_path,
                    "analysis": analysis
                })
                
            except Exception as e:
                print(f"[Error] Failed to analyze image {full_path}: {e}")
                await self._log(f"Failed to analyze image {image_path}: {e}", node_id=req_id, status="error")
                
        # 2. Update database
        if visual_references:
            update_requirement_visuals(req_id, visual_references)
            await self._log(f"Stored {len(visual_references)} visual references for {req_id}", node_id=req_id)
    

def visual_analysis_prompt() -> str:
    return """
**CRITICAL ROLE:** You are a "Headless" Frontend Reverse-Engineer.
**SCENARIO:** You must describe this UI screenshot to a blind developer who CANNOT see the image. They must reconstruct this page **pixel-perfectly** and **content-perfectly** using only your text description.

**CORE DIRECTIVES:**
1.  **FULL OCR TRANSCRIPTION:** You MUST transcribe **ALL** visible text content exactly as it appears. Do not summarize text.
2.  **STRICT DOM HIERARCHY:** Describe the layout as a tree structure (Parent -> Child -> Sibling).
3.  **PRECISE VISUAL SPECS:** Specify Geometry (px), Layout (Flex/Grid), Style (Hex colors), and Typography.

**OUTPUT FORMAT (Strict Markdown Tree):**

### 1. Global Design Tokens
* **Colors:** Define Primary, Secondary, Backgrounds (Estimate Hex).
* **Font:** Suggest font stack.

### 2. Page Structure & Content (Iterate from Top to Bottom)

#### [A] [Section Name] (e.g., Header, Sidebar, Card)
* **Container:** Dimensions, background color, layout properties.
* **Child Element 1:** [Type: Navigation/List]
    * **Layout:** Flex-row, gap 20px.
    * **Items (Transcription Examples):**
        * *If English:* "Home", "Products", "Contact Us" (Bold, Black).
        * *If Chinese:* "首页", "产品中心", "联系我们" (Regular, Gray).
* **Child Element 2:** [Type: Form Component]
    * **Container Style:** Border, shadow, padding.
    * **Internal Layout:** Vertical stack.
    * **Content (Transcription Examples):**
        * **Label:** "Username" OR "用户名" (Exact text).
        * **Input Placeholder:** "Enter your email..." OR "请输入邮箱地址..." (Exact text).
        * **Button:** "Submit" OR "立即提交" (White text on Blue bg).
* **Child Element 3:** [Type: Banner/Hero]
    * **Headline:** "Build Faster" OR "极速构建" (Font size ~32px, Bold).
    * **Sub-text:** "Start your journey today." OR "开启您的数字化之旅。" (Gray, ~16px).

**Action:** Start the "Blind Transcription". Ensure EVERY character (CN/EN) visible in the image is recorded in your description.
"""

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
        print(f"[Error] Failed to parse JSON for Node {req_id}: {e}")
        return []
