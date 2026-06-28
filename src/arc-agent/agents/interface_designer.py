import asyncio
import base64
import json
import mimetypes
import os
import re
from typing import List, Dict, Any
from urllib.parse import urlparse

import requests

from .arc_agent import ARCAgent
from traceability.database import update_requirement_visuals

class InterfaceDesigner(ARCAgent):
    def __init__(self, log_cb=None):
        super().__init__(
            agent_name="InterfaceDesigner",
            log_cb=log_cb
        )

    @staticmethod
    def build_visual_analysis_prompt() -> str:
        return """
**CRITICAL ROLE:** You are a "Headless" Frontend Reverse-Engineer.
**SCENARIO:** You must describe this UI screenshot to a blind developer who CANNOT see the image. They must reconstruct this page pixel-perfectly and content-perfectly using only your text description.

**CORE DIRECTIVES:**
1. **FULL OCR TRANSCRIPTION:** You MUST transcribe ALL visible text content exactly as it appears. Do not summarize text.
2. **STRICT DOM HIERARCHY:** Describe the layout as a tree structure (Parent -> Child -> Sibling).
3. **PRECISE VISUAL SPECS:** Specify Geometry (px), Layout (Flex/Grid), Style (Hex colors), and Typography.

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
        * If English: "Home", "Products", "Contact Us" (Bold, Black).
        * If Chinese: "Shouye", "Chanpin Zhongxin", "Lianxi Women" (Regular, Gray).
* **Child Element 2:** [Type: Form Component]
    * **Container Style:** Border, shadow, padding.
    * **Internal Layout:** Vertical stack.
    * **Content (Transcription Examples):**
        * **Label:** "Username" OR "Yonghu Ming" (Exact text).
        * **Input Placeholder:** "Enter your email..." OR "Qingshuru Youxiang Dizhi..." (Exact text).
        * **Button:** "Submit" OR "Liji Tijiao" (White text on Blue bg).
* **Child Element 3:** [Type: Banner/Hero]
    * **Headline:** "Build Faster" OR "Jisu Goujian" (Font size ~32px, Bold).
    * **Sub-text:** "Start your journey today." OR "Kaiqi Ninde Shuzihua Zhilu." (Gray, ~16px).

**Action:** Start the blind transcription. Ensure every visible CN/EN character is recorded in your description.
"""

    @staticmethod
    def resolve_visual_api_key() -> str:
        return os.environ.get("VISUAL_API_KEY") or os.environ.get("OPENAI_API_KEY", "")

    @staticmethod
    def resolve_visual_api_url() -> str:
        raw_url = (
            os.environ.get("VISUAL_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL", "")
        ).strip()
        if not raw_url:
            return ""

        parsed = urlparse(raw_url)
        normalized_path = parsed.path.rstrip("/")
        if normalized_path.endswith("/chat/completions"):
            return raw_url.rstrip("/")

        if not normalized_path:
            normalized_path = "/chat/completions"
        else:
            normalized_path = f"{normalized_path}/chat/completions"

        return parsed._replace(path=normalized_path).geturl()

    async def parse_and_store_visual_elements(
        self,
        workspace_path: str,
        requirement_data: dict[str, Any],
    ) -> None:
        description = requirement_data.get("description", "")
        req_id = requirement_data.get("req_id") or requirement_data.get("id", "")
        if not description or not req_id:
            if self.log_cb:
                await self.log_cb("System", "Invalid requirement data", "error", req_id or None)
            return

        matches = re.findall(r"!\[image\]\(([^)]+)\)", description)
        if not matches:
            if self.log_cb:
                await self.log_cb("System", "No image found in the description.", "info", req_id)
            return

        visual_references = []
        for image_path in matches:
            normalized_path = os.path.normpath(image_path)
            if normalized_path.startswith(os.sep):
                normalized_path = normalized_path.lstrip(os.sep)

            full_path = os.path.abspath(os.path.join(workspace_path, normalized_path))
            if not os.path.exists(full_path):
                if self.log_cb:
                    await self.log_cb("System", f"Image not found: {full_path}", "warning", req_id)
                continue

            try:
                mime_type, _ = mimetypes.guess_type(full_path)
                if not mime_type:
                    mime_type = "image/png"

                with open(full_path, "rb") as image_file:
                    base64_image = base64.b64encode(image_file.read()).decode("utf-8")

                if self.log_cb:
                    await self.log_cb("System", f"Analyzing visual element: {image_path}", None, req_id)

                visual_api_url = self.resolve_visual_api_url()
                visual_api_key = self.resolve_visual_api_key()
                if not visual_api_url:
                    raise Exception("Visual API base URL is not configured.")
                if not visual_api_key:
                    raise Exception("Visual API key is not configured.")

                response = await asyncio.to_thread(
                    requests.post,
                    visual_api_url,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {visual_api_key}",
                    },
                    data=json.dumps(
                        {
                            "model": os.environ.get("VISUAL_MODEL", os.environ.get("MODEL", "")),
                            "messages": [
                                {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": self.build_visual_analysis_prompt()},
                                        {
                                            "type": "image_url",
                                            "image_url": {
                                                "url": f"data:{mime_type};base64,{base64_image}",
                                            },
                                        },
                                    ],
                                }
                            ],
                        }
                    ),
                    verify=False,
                )

                if response.status_code != 200:
                    raise Exception(f"ModelArts API Error {response.status_code}: {response.text}")

                response_data = response.json()
                analysis = response_data["choices"][0]["message"]["content"]
                visual_references.append({"image_path": image_path, "analysis": analysis})
            except Exception as exc:
                if self.log_cb:
                    await self.log_cb("System", f"Failed to analyze image {image_path}: {exc}", "error", req_id)

        if visual_references:
            update_requirement_visuals(req_id, visual_references)
            if self.log_cb:
                await self.log_cb(
                    "System",
                    f"Stored {len(visual_references)} visual references for {req_id}",
                    None,
                    req_id,
                )

    def get_system_prompt(self) -> str:
        from utils import get_app_type, get_android_package
        app_type = get_app_type()
        android_pkg = ""
        pkg_compliance = ""

        if app_type == "android":
            android_pkg = get_android_package()
            pkg_compliance = f"""
### Package Compliance (CRITICAL for Android):
- The application package is `{android_pkg}`. You MUST use this package for ALL generated code:
  - `package {android_pkg};` in every Java file
  - `import {android_pkg}.xxx;` for cross-module references
  - File paths must use `{android_pkg.replace('.', '/')}/` as the package directory
  - AndroidManifest.xml must reference activities as `{android_pkg}.ActivityName`
- Do NOT use `com.example.template` or any other package name.
- If the requirement description mentions a different package name in resource-id patterns (e.g., `org.billthefarmer.editor:id/newFile`), use THAT package name instead of `{android_pkg}`. The resource-id package takes priority.
"""
        else:
            pkg_compliance = ""

        return f"""You are a Principal Software Architect.
Your task is to analyze a raw software requirement and design its interfaces (UI -> API -> FUNC -> DB).

For **non-leaf nodes**: design the shared foundation and aggregation layer for child nodes. Focus on reusable infrastructure such as page shells, routing/entry points, shared state, common data contracts, shared services, and base storage/models. Keep the design modular so leaf nodes can extend it with concrete feature logic.

For **leaf nodes**: design ALL layers with real logic (not just `throw UnsupportedOperationException`). Use actual DAO calls, return real data, wire up LiveData/queries.

{pkg_compliance}

Design constraints (strict):
- Prefer stable, deterministic module boundaries. One interface = one clear responsibility.
- Interface IDs must be stable and explicit: `IF_{{TYPE}}_{{DOMAIN}}_{{ACTION}}` (e.g., `IF_API_USER_LOGIN`).
- Keep contracts backward-compatible when reusing interfaces; use optional params for extensions.
- Do not invent dependency interfaces if they already exist in traceability search results.
- **UI Resource-ID Compliance**: If the requirement description or scenarios specify exact `resource-id` values (e.g., `org.billthefarmer.editor:id/newFile`), you MUST use those exact IDs when designing UI interfaces. The `android:id` in XML layouts and `findViewById(R.id.xxx)` in Java must match the resource-id suffix specified in the scenarios. This is critical for automated testing to find the UI elements.

# Workflow:
1. **Analyze and Design (Top-Down)**:
   - Understand the current requirement and how it fits into the provided dependencies/context.
   - **Extract Resource-IDs**: If the requirement description contains `resource-id` references (e.g., `` `pkg:id/buttonName` ``), extract and record them. These MUST be used as the actual `android:id` values in the generated UI code.
   - Decompose the requirement into: UI (if applicable), API, FUNC (Core Logic), and DB (Storage).
   - **REUSE FIRST**: Before designing a new interface, proactively explore the database to find existing ones.
     - Use `search_interfaces_by_keyword` to find logic by name (e.g., 'auth', 'payment').
     - Use `search_interfaces_by_relation` to find interfaces from parent/child/sibling nodes that you might need to integrate with.
2. **Interface Reuse Mechanism**:
   - If an existing interface perfectly matches your needs, mark it for reuse by setting `"reuse": true` and providing its exact existing `"interface_id"`.
   - If an existing interface needs slight modification, you MUST first call `find_interface_impacts` to see what other interfaces call it.

# CRITICAL Output Requirement:
You MUST output a single JSON array in a markdown block (` ```json ... ``` `).
This JSON represents the Intermediate Representation (IR) mapping of the interfaces you designed or reused.
Do NOT write any code files yet — this phase is ONLY for designing the interface architecture.
Each object in the array must follow this exact schema:
{{
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
}}
"""

    def get_tool_names(self) -> List[str]:
        return [
            "read_file", "list_directory", "glob", "grep",
            "search_interfaces_by_keyword", "search_interfaces_by_relation",
            "find_interface_impacts", "get_node_relations"
        ]

    def _get_implement_tool_names(self) -> List[str]:
        return [
            "read_file", "write_file", "edit_file", "delete_file",
            "list_directory", "glob", "grep", "run_build",
            "search_interfaces_by_keyword", "search_interfaces_by_relation",
            "find_interface_impacts", "get_node_relations"
        ]

    async def design_ir(self, node_id: str, requirement_data: dict, is_leaf: bool = True) -> tuple:
        """Phase 1: Design interfaces and output IR JSON. No code writing.
        Returns (ir_output, messages) so the session can be continued by implement_stubs_from_session().
        """
        from .context_pipeline import context_pipeline

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(node_id=node_id, agent_type=self.agent_name)

        if is_leaf:
            scope_guidance = """
### Node Scope: LEAF NODE (Full Implementation)
This is a **leaf node** (no children). Design interfaces for ALL layers:
- **DB layer**: Room entities, DAOs (only if not already created by a parent node)
- **API layer**: Repositories / Services
- **FUNC layer**: ViewModels / UseCases
- **UI layer**: Activities, Fragments, Adapters, XML layouts

Implement real logic (not just `throw UnsupportedOperationException`). Use actual DAO/Repository calls, return real data from LiveData/queries.
"""
        else:
            scope_guidance = """
### Node Scope: NON-LEAF NODE (Shared Foundation & Aggregation)
This is a **non-leaf node** (it has children). Design the shared foundation and aggregation layer that child nodes will extend:
- Page shells, routing/entry points, shared state, common data contracts, shared services, and base storage/models.
- Also land concrete stub interface skeletons (signatures + placeholder returns) for these shared components so child nodes can import and refine them.
- Keep the design modular; do NOT implement full feature logic — that belongs to leaf nodes.
"""

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}

{scope_guidance}

Perform the top-down decomposition for Node [{node_id}].
Use the `<current_requirement>` block in the prefetched context as the authoritative current-node payload.
Design the interface architecture and output the IR JSON mapping.
Do NOT write any code files — this phase is ONLY for architecture design.
"""
        system_content = self.get_system_prompt()
        if static_ctx:
            system_content = f"{system_content}\n\n{static_ctx}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt}
        ]
        from .tools import TOOL_REGISTRY
        tools = [TOOL_REGISTRY[n]["schema"] for n in self.get_tool_names() if n in TOOL_REGISTRY]
        result, messages = await self.run_from_messages(messages, node_id=node_id, max_steps=10, tools=tools)
        return result, messages

    async def implement_stubs_from_session(self, messages: List[Dict], interfaces: List[Dict], node_id: str, is_leaf: bool = True) -> tuple:
        """Phase 2: Continue from design_ir's session to implement stub code.
        The LLM already understands the architecture from design_ir, so it can
        start writing code immediately without re-reading/re-understanding the context.
        Returns (output, messages).
        """
        from .context_pipeline import context_pipeline
        from utils import get_app_type, get_android_package
        from .tools import TOOL_REGISTRY

        # Switch to implementation tool set (adds write_file, run_build, etc.)
        impl_tools = [TOOL_REGISTRY[n]["schema"] for n in self._get_implement_tool_names() if n in TOOL_REGISTRY]

        # Build a summary of what needs to be implemented
        iface_summaries = []
        for iface in interfaces:
            iface_summaries.append(
                f"- [{iface.get('interface_id')}] Type: {iface.get('type')} "
                f"File: `{iface.get('file_path', 'TBD')}` "
                f"Name: {iface.get('name', '')} "
                f"Desc: {iface.get('description', '')}"
            )

        # Package compliance for Android
        pkg_compliance = ""
        if get_app_type() == "android":
            android_pkg = get_android_package()
            pkg_compliance = f"""
### Package Compliance (CRITICAL):
- Use `package {android_pkg};` in every Java file.
- Use `import {android_pkg}.xxx;` for cross-module references.
- Place files under `app/src/main/java/{android_pkg.replace('.', '/')}/`.
- Do NOT use `com.example.template` or any other package.
- If the requirement description mentions a resource-id with a different package (e.g., `org.billthefarmer.editor:id/newFile`), use THAT package instead.
"""

        if is_leaf:
            impl_guidance = f"""
### Implementation Scope: LEAF NODE
Implement ALL interfaces with real logic. Use actual DAO calls, return real data.
Do NOT use `throw UnsupportedOperationException` — implement working code.
**UI Resource-ID Compliance**: When writing XML layouts, use the exact `android:id` values specified in the requirement description/scenarios (e.g., if a scenario says `org.billthefarmer.editor:id/newFile`, the XML must have `android:id="@+id/newFile"`). When writing Java code, use `findViewById(R.id.newFile)` with matching IDs.
After writing all files, call `run_build` to verify compilation. Fix any errors.
{pkg_compliance}
"""
        else:
            impl_guidance = f"""
### Implementation Scope: NON-LEAF NODE
Implement ONLY the DB layer interfaces (Entity/DAO/AppDatabase).
Do NOT create Repository/ViewModel/Fragment/Layout files.
After writing all files, call `run_build` to verify compilation. Fix any errors.
{pkg_compliance}
"""

        impl_prompt = f"""
### Implementation Task for Node [{node_id}]
{impl_guidance}

### Interfaces to Implement ({len(interfaces)} total):
{chr(10).join(iface_summaries)}

### Full Interface Definitions:
```json
{json.dumps(interfaces, indent=2, ensure_ascii=False)}
```

Write ALL stub code files using `write_file` calls FIRST, then call `run_build` ONCE to verify compilation.
Do NOT call `read_file` on source files — you already have the context from the previous design phase.
Do NOT interleave `read_file` and `write_file` — batch all writes together.
Ensure all imports, class hierarchies, and method signatures match the interface definitions above.
Fix any build errors found using `edit_file` (provide exact old_string/new_string for precise replacements).
When all files are written and compilation passes, output "IMPLEMENTED".
"""
        # Append implementation prompt to the existing session
        messages.append({"role": "user", "content": impl_prompt})
        result, messages = await self.run_from_messages(messages, node_id=node_id, max_steps=20, tools=impl_tools)
        return result, messages
