import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
from typing import Any, Dict, List
from urllib.parse import urlparse

import requests

from .arc_agent import ARCAgent
from .prompt_sections import get_common_session_guidance, get_interface_designer_guidance
from traceability.database import update_requirement_visuals
from utils import read_json_file, write_json_file


class InterfaceDesigner(ARCAgent):
    def __init__(self, log_cb=None):
        super().__init__(
            agent_name="InterfaceDesigner",
            log_cb=log_cb,
        )
        self._non_leaf_existing_write_guard = False
        self._non_leaf_no_write_guard = False

    async def _intercept_tool_call(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        node_id: str | None = None,
    ) -> tuple[bool, Any]:
        if self._non_leaf_existing_write_guard and tool_name == "write_file":
            path = str(tool_args.get("path", "")).strip()
            if path and os.path.exists(path):
                return True, (
                    f"Error: `{path}` already exists. In non-leaf convergence, do not overwrite existing shared files "
                    "with `write_file`. Read the file and use `edit_file` for a minimal targeted change instead."
                )
        return await super()._intercept_tool_call(tool_name, tool_args, node_id)

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
    def _visual_cache_path(workspace_path: str) -> str:
        return os.path.join(workspace_path, ".arc", "visual_analysis_cache.json")

    @classmethod
    def _load_visual_cache(cls, workspace_path: str) -> dict[str, Any]:
        return read_json_file(cls._visual_cache_path(workspace_path)) or {}

    @classmethod
    def _save_visual_cache(cls, workspace_path: str, cache: dict[str, Any]) -> None:
        write_json_file(cls._visual_cache_path(workspace_path), cache)

    @staticmethod
    def _build_visual_cache_key(full_path: str) -> str:
        stat = os.stat(full_path)
        raw_key = f"{os.path.abspath(full_path)}::{int(stat.st_mtime_ns)}::{stat.st_size}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

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
        requirements_dir: str,
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

        visual_cache = self._load_visual_cache(workspace_path)
        cache_updated = False
        visual_references = []
        for image_path in matches:
            normalized_path = os.path.normpath(image_path)
            if normalized_path.startswith(os.sep):
                normalized_path = normalized_path.lstrip(os.sep)

            base_dir = requirements_dir if requirements_dir else workspace_path
            full_path = os.path.abspath(os.path.join(base_dir, normalized_path))
            if not os.path.exists(full_path):
                if self.log_cb:
                    await self.log_cb("System", f"Image not found: {full_path}", "warning", req_id)
                continue

            try:
                cache_key = self._build_visual_cache_key(full_path)
                cached_entry = visual_cache.get(cache_key)
                if cached_entry and cached_entry.get("analysis"):
                    visual_references.append({"image_path": image_path, "analysis": cached_entry["analysis"]})
                    if self.log_cb:
                        await self.log_cb("System", f"Reusing cached visual analysis: {image_path}", None, req_id)
                    continue

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
                visual_cache[cache_key] = {
                    "image_path": image_path,
                    "full_path": os.path.abspath(full_path),
                    "analysis": analysis,
                }
                cache_updated = True
                visual_references.append({"image_path": image_path, "analysis": analysis})
            except Exception as exc:
                if self.log_cb:
                    await self.log_cb("System", f"Failed to analyze image {image_path}: {exc}", "error", req_id)

        if cache_updated:
            self._save_visual_cache(workspace_path, visual_cache)

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
        from utils import get_android_package, get_app_type, get_web_base_url, get_web_port

        app_type = get_app_type()
        if app_type == "android":
            android_pkg = get_android_package()
            runtime_rules = f"""
### Android package rules
- Use `{android_pkg}` consistently in source paths and package declarations.
- If the requirement or scenario gives an explicit resource-id package, that package wins for the ids it names.
"""
        else:
            runtime_rules = f"""
### Web runtime rules
- The only deployed web runtime is the backend on port `{get_web_port()}` at `{get_web_base_url()}`.
- The backend serves `frontend/dist` on the same origin.
- Prefer existing app shells, route containers, layouts, providers, pages, TS/TSX modules, and Tailwind-based UI composition.
"""

        return f"""You are the interface design agent for this compiler.
Produce compact, structured design artifacts that the next agents can execute against.

Rules:
- Reuse existing interfaces before inventing new ones.
- Preserve `<frozen_node_contract>` unless the requirement explicitly forces a change.
- Respect `<current_requirement>`, `<node_understanding>`, visual analysis, and scenario details.
- If the requirement names exact UI ids or resource ids, keep them exact.
- For leaf work, design the smallest complete chain needed across UI -> API -> FUNC -> DB.
- For non-leaf work, stay at parent UI shell scope: routes, layouts, providers, page containers, mount points, and thin composition boundaries.
- During understanding / IR design / spec generation, do not write code files.
- After IR and spec are produced, you must materialize the design into code:
- UI interfaces owned by the current node must be implemented as real UI code, not left as empty stubs.
- Non-UI interfaces owned by the current node must be landed as minimal compilable code skeletons or stubs aligned with the spec.
- Prefer extending existing files with minimal edits over creating parallel files.

{get_common_session_guidance()}

{get_interface_designer_guidance()}

{runtime_rules}
"""

    def get_tool_names(self) -> List[str]:
        return [
            "read_file",
            "list_directory",
            "glob",
            "grep",
            "search_interfaces_by_keyword",
            "search_interfaces_by_relation",
            "find_interface_impacts",
            "get_node_relations",
        ]

    def _get_implement_tool_names(self) -> List[str]:
        return [
            "read_file",
            "write_file",
            "edit_file",
            "delete_file",
            "list_directory",
            "glob",
            "grep",
            "run_build",
            "search_interfaces_by_keyword",
            "search_interfaces_by_relation",
            "find_interface_impacts",
            "get_node_relations",
        ]

    def _get_non_leaf_audit_tool_names(self) -> List[str]:
        return [
            "read_file",
            "write_file",
            "edit_file",
            "delete_file",
            "list_directory",
            "glob",
            "grep",
            "run_build",
            "search_interfaces_by_keyword",
            "search_interfaces_by_relation",
            "find_interface_impacts",
            "get_node_relations",
        ]

    @staticmethod
    def _extract_json_object_from_markdown(raw_output: str) -> dict[str, Any] | None:
        if not raw_output:
            return None
        fenced = re.search(r"```json\s*(.*?)\s*```", raw_output, re.DOTALL | re.IGNORECASE)
        candidate = fenced.group(1) if fenced else raw_output
        try:
            data = json.loads(candidate)
            return data if isinstance(data, dict) else None
        except Exception:
            pass
        span = re.search(r"(\{\s*\"[\s\S]*\})", raw_output)
        if not span:
            return None
        try:
            data = json.loads(span.group(1))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    @staticmethod
    def _extract_json_array_from_markdown(raw_output: str) -> list[dict[str, Any]] | None:
        if not raw_output:
            return None
        fenced = re.search(r"```json\s*(.*?)\s*```", raw_output, re.DOTALL | re.IGNORECASE)
        candidate = fenced.group(1) if fenced else raw_output
        try:
            data = json.loads(candidate)
            return data if isinstance(data, list) else None
        except Exception:
            pass
        span = re.search(r"(\[\s*{[\s\S]*}\s*\])", raw_output)
        if not span:
            return None
        try:
            data = json.loads(span.group(1))
            return data if isinstance(data, list) else None
        except Exception:
            return None

    @staticmethod
    def _build_design_scope_guidance(design_mode: str) -> str:
        if design_mode == "leaf_full":
            return """
### Scope
- This is a leaf node.
- Decompose the feature from UI to API to FUNC to DB.
- Reuse existing interfaces where possible, and only add the minimum new contracts needed to land the feature.
"""
        if design_mode == "non_leaf_ui_with_shell_tests":
            return """
### Scope
- This is a non-leaf parent node.
- Design only parent UI shell interfaces: top-level routes, layouts, providers, mount points, and thin composition boundaries.
- The result must support parent shell testing, but it must not invent API/FUNC/DB layers unless the requirement text makes it unavoidable.
"""
        return """
### Scope
- This is a non-leaf parent node.
- Design only parent UI shell interfaces: top-level routes, layouts, providers, mount points, and thin composition boundaries.
- Do not design API/FUNC/DB interfaces in this mode.
"""

    @staticmethod
    def _build_fallback_understanding(requirement_data: dict[str, Any]) -> dict[str, Any]:
        scenarios = requirement_data.get("scenarios") or []
        return {
            "summary": str(requirement_data.get("description", "") or "")[:500],
            "dependencies": requirement_data.get("dependencies") or [],
            "scenario_summary": [
                {
                    "scenario_id": scenario.get("scenario_id") or scenario.get("id", ""),
                    "name": scenario.get("name", ""),
                    "steps": scenario.get("steps", [])[:4],
                }
                for scenario in scenarios[:2]
            ],
            "relevant_files": [],
            "reuse_candidates": [],
            "risks": ["Understanding fell back to deterministic requirement parsing."],
        }

    @staticmethod
    def _build_fallback_interface_spec(
        interfaces: list[dict[str, Any]],
        node_understanding: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        reuse_constraints = []
        if node_understanding and node_understanding.get("reuse_candidates"):
            reuse_constraints.append("Check existing related interfaces before adding a new contract.")

        specs: list[dict[str, Any]] = []
        for interface in interfaces:
            if not isinstance(interface, dict):
                continue
            specs.append(
                {
                    "interface_id": str(interface.get("interface_id", "")).strip(),
                    "type": str(interface.get("type", "")).strip(),
                    "file_path": str(interface.get("file_path", "")).strip(),
                    "first_line": str(interface.get("first_line", "")).strip(),
                    "responsibility": str(interface.get("description", "") or "").strip(),
                    "inputs": interface.get("inputs", []) if isinstance(interface.get("inputs"), list) else [],
                    "outputs": interface.get("outputs", []) if isinstance(interface.get("outputs"), list) else [],
                    "preconditions": [],
                    "postconditions": [],
                    "error_cases": [],
                    "reuse_constraints": list(reuse_constraints),
                }
            )
        return specs

    def _build_fallback_leaf_design_bundle(
        self,
        requirement_data: dict[str, Any],
        interfaces: list[dict[str, Any]] | None = None,
        node_understanding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_understanding = node_understanding or self._build_fallback_understanding(requirement_data)
        resolved_interfaces = interfaces or []
        return {
            "node_understanding": resolved_understanding,
            "interfaces": resolved_interfaces,
            "interface_spec": self._build_fallback_interface_spec(resolved_interfaces, resolved_understanding),
        }

    async def understand_node(
        self,
        node_id: str,
        requirement_data: dict[str, Any],
        design_mode: str = "leaf_full",
        preloaded_source: str = None,
    ) -> tuple[dict[str, Any], list]:
        from .context_pipeline import context_pipeline
        from .tools import TOOL_REGISTRY

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
        )
        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}

### Task
Understand this node before interface design.
{self._build_design_scope_guidance(design_mode)}

Return exactly one JSON object in a `json` markdown block with this schema:
{{
  "requirement_summary": {{
    "name": "short name",
    "description": "1-3 sentence summary",
    "dependencies": ["dependency ids"]
  }},
  "scenario_summary": [
    {{
      "scenario_id": "id",
      "name": "scenario name",
      "steps": ["important step", "..."]
    }}
  ],
  "visual_summary": [
    {{
      "image_path": "path",
      "analysis": "short visual summary"
    }}
  ],
  "relevant_files": ["most relevant existing files"],
  "reuse_candidates": [
    {{
      "interface_id": "existing interface id",
      "type": "UI/API/FUNC/DB",
      "reason": "why it may be reused"
    }}
  ],
  "risks": ["main design or integration risks"]
}}

Keep it concrete. Favor existing code and existing interfaces over speculation.
"""
        system_content = self.get_system_prompt()
        if static_ctx:
            system_content = f"{system_content}\n\n{static_ctx}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]
        tools = [TOOL_REGISTRY[name]["schema"] for name in self.get_tool_names() if name in TOOL_REGISTRY]
        raw_output, messages = await self.run_from_messages(messages, node_id=node_id, max_steps=8, tools=tools)
        parsed = self._extract_json_object_from_markdown(raw_output)
        if not parsed:
            parsed = self._build_fallback_understanding(requirement_data)
        return parsed, messages

    async def design_leaf_bundle(
        self,
        node_id: str,
        requirement_data: dict[str, Any],
        preloaded_source: str = None,
    ) -> tuple[dict[str, Any], list]:
        from .context_pipeline import context_pipeline
        from .tools import TOOL_REGISTRY

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
        )
        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}

### Task
This is a leaf node. In one pass:
1. Understand the requirement and the most relevant existing code.
2. Design the smallest interface chain needed across UI -> API -> FUNC -> DB.
3. Turn each interface into an executable behavioral specification.

{self._build_design_scope_guidance("leaf_full")}

Return exactly one JSON object in a `json` markdown block with this schema:
{{
  "node_understanding": {{
    "summary": "1-3 short sentences",
    "dependencies": ["dependency ids"],
    "scenario_summary": [
      {{
        "scenario_id": "id",
        "name": "scenario name",
        "steps": ["important step", "..."]
      }}
    ],
    "relevant_files": ["most relevant existing files"],
    "reuse_candidates": [
      {{
        "interface_id": "existing interface id",
        "type": "UI/API/FUNC/DB",
        "reason": "why it may be reused"
      }}
    ],
    "risks": ["main design or integration risks"]
  }},
  "interfaces": [
    {{
      "interface_id": "stable explicit id",
      "reuse": true,
      "type": "UI/API/FUNC/DB",
      "name": "logical module name",
      "description": "purpose",
      "inputs": ["inputs"],
      "outputs": ["outputs"],
      "callers": ["caller interface ids"],
      "callees": ["callee interface ids"],
      "file_path": "relative file path",
      "first_line": "exact signature or declaration line"
    }}
  ],
  "interface_spec": [
    {{
      "interface_id": "exact interface id",
      "type": "UI/API/FUNC/DB",
      "file_path": "relative path",
      "first_line": "definition signature",
      "responsibility": "what this interface must do",
      "inputs": ["input contract"],
      "outputs": ["output contract"],
      "preconditions": ["required state before call or render"],
      "postconditions": ["observable result after success"],
      "error_cases": ["important failure or edge cases"],
      "reuse_constraints": ["rules for reusing or extending existing code"]
    }}
  ]
}}

Rules:
- Reuse existing interfaces whenever possible.
- Keep the interface chain minimal and executable.
- Make `interface_spec` align exactly with `interfaces`.
- Keep the output compact:
- `scenario_summary`: at most 2 items, each with at most 4 key steps.
- `relevant_files`: at most 5.
- `reuse_candidates`: at most 3.
- In `interface_spec`, keep each array concise and only include requirements that matter for testing or implementation.
- Prefer short phrases over full paragraphs except for `summary`.
- Do not write code in this step.
"""
        system_content = self.get_system_prompt()
        if static_ctx:
            system_content = f"{system_content}\n\n{static_ctx}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]
        tools = [TOOL_REGISTRY[name]["schema"] for name in self.get_tool_names() if name in TOOL_REGISTRY]
        raw_output, messages = await self.run_from_messages(messages, node_id=node_id, max_steps=12, tools=tools)
        parsed = self._extract_json_object_from_markdown(raw_output) or {}

        node_understanding = parsed.get("node_understanding")
        interfaces = parsed.get("interfaces")
        interface_spec = parsed.get("interface_spec")

        if not isinstance(node_understanding, dict):
            node_understanding = self._build_fallback_understanding(requirement_data)
        if not isinstance(interfaces, list):
            interfaces = []
        if not isinstance(interface_spec, list):
            interface_spec = self._build_fallback_interface_spec(interfaces, node_understanding)

        return {
            "node_understanding": node_understanding,
            "interfaces": interfaces,
            "interface_spec": interface_spec,
        }, messages

    async def build_interface_spec(
        self,
        node_id: str,
        requirement_data: dict[str, Any],
        interfaces: list[dict[str, Any]],
        node_understanding: dict[str, Any] | None,
        design_mode: str = "leaf_full",
        preloaded_source: str = None,
    ) -> tuple[list[dict[str, Any]], list]:
        from .context_pipeline import context_pipeline
        from .tools import TOOL_REGISTRY

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
        )
        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}

### Node Understanding
```json
{json.dumps(node_understanding or {}, indent=2, ensure_ascii=False)}
```

### Interface IR
```json
{json.dumps(interfaces, indent=2, ensure_ascii=False)}
```

### Task
Build the executable specification for each interface.
{self._build_design_scope_guidance(design_mode)}

Return exactly one JSON array in a `json` markdown block.
Each item must follow this schema:
{{
  "interface_id": "exact interface id",
  "type": "UI/API/FUNC/DB",
  "file_path": "relative path",
  "first_line": "definition signature",
  "responsibility": "what this interface must do",
  "inputs": ["input contract"],
  "outputs": ["output contract"],
  "preconditions": ["required state before call or render"],
  "postconditions": ["observable result after success"],
  "error_cases": ["important failure or edge cases"],
  "reuse_constraints": ["rules for reusing or extending existing code"]
}}

Do not restate the IR mechanically. Turn it into testable behavioral contracts.
"""
        system_content = self.get_system_prompt()
        if static_ctx:
            system_content = f"{system_content}\n\n{static_ctx}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]
        tools = [TOOL_REGISTRY[name]["schema"] for name in self.get_tool_names() if name in TOOL_REGISTRY]
        raw_output, messages = await self.run_from_messages(messages, node_id=node_id, max_steps=8, tools=tools)
        parsed = self._extract_json_array_from_markdown(raw_output)
        if not parsed:
            parsed = self._build_fallback_interface_spec(interfaces, node_understanding)
        return parsed, messages

    async def design_ir(
        self,
        node_id: str,
        requirement_data: dict,
        design_mode: str = "leaf_full",
    ) -> tuple:
        from .context_pipeline import context_pipeline
        from .tools import TOOL_REGISTRY

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
        )
        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}

{self._build_design_scope_guidance(design_mode)}

### Task
Design the interface IR for this node.
- Use `<current_requirement>` as the authoritative task payload.
- Reuse existing interfaces whenever possible.
- Respect `<node_understanding>` and `<frozen_node_contract>` if present.
- In non-leaf modes, stay at parent shell scope and avoid API/FUNC/DB expansion unless the requirement text makes it unavoidable.

Return exactly one JSON array in a `json` markdown block.
Each object must follow:
{{
  "interface_id": "stable explicit id",
  "reuse": true or false,
  "type": "UI/API/FUNC/DB",
  "name": "logical module name",
  "description": "purpose",
  "inputs": ["inputs"],
  "outputs": ["outputs"],
  "callers": ["caller interface ids"],
  "callees": ["callee interface ids"],
  "file_path": "relative file path",
  "first_line": "exact signature or declaration line"
}}
"""
        system_content = self.get_system_prompt()
        if static_ctx:
            system_content = f"{system_content}\n\n{static_ctx}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]
        tools = [TOOL_REGISTRY[name]["schema"] for name in self.get_tool_names() if name in TOOL_REGISTRY]
        result, messages = await self.run_from_messages(messages, node_id=node_id, max_steps=10, tools=tools)
        return result, messages

    async def materialize_interfaces(
        self,
        node_id: str,
        requirement_data: dict[str, Any],
        interfaces: list[dict[str, Any]],
        interface_spec: list[dict[str, Any]],
        node_understanding: dict[str, Any] | None,
        design_mode: str = "leaf_full",
        preloaded_source: str = None,
    ) -> tuple[str, list]:
        from .context_pipeline import context_pipeline
        from .tools import TOOL_REGISTRY

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
        )

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}

### Node Understanding
```json
{json.dumps(node_understanding or {}, indent=2, ensure_ascii=False)}
```

### Interface IR
```json
{json.dumps(interfaces, indent=2, ensure_ascii=False)}
```

### Interface Specification
```json
{json.dumps(interface_spec, indent=2, ensure_ascii=False)}
```

### Task
Materialize the current node's interfaces into code.
{self._build_design_scope_guidance(design_mode)}

Execution rules:
- This is a code-writing step.
- For every current-node UI interface, land real UI code now. Do not leave TODO-only shells, placeholder divs, or interface-only JSON.
- For every current-node non-UI interface, land the smallest compilable or runnable skeleton that matches the specification.
- Reuse and minimally edit existing files when possible. If a target file already exists, read it first and use `edit_file` unless a full rewrite is clearly simpler and still local.
- Keep the implementation shallow: UI can be complete, but API/FUNC/DB should usually be skeletal scaffolding unless the requirement text already demands more.
- Preserve the declared file paths and ownership boundaries from the interface IR.
- Call `run_build` once after landing the materialized code.

When finished, output exactly one JSON array in a `json` markdown block.
Each item must follow this schema:
{{
  "interface_id": "exact interface id",
  "implemented": true,
  "file_path": "relative path",
  "first_line": "exact first line now present in code",
  "kind": "ui_complete|code_skeleton",
  "summary": "what was landed"
}}
"""
        system_content = self.get_system_prompt()
        if static_ctx:
            system_content = f"{system_content}\n\n{static_ctx}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]
        tools = [TOOL_REGISTRY[name]["schema"] for name in self._get_implement_tool_names() if name in TOOL_REGISTRY]
        result, messages = await self.run_from_messages(messages, node_id=node_id, max_steps=20, tools=tools)
        return result, messages

    async def converge_non_leaf(
        self,
        node_id: str,
        interfaces: List[Dict[str, Any]],
        convergence_summary: str,
        preloaded_source: str = None,
    ) -> tuple:
        from .context_pipeline import context_pipeline
        from .tools import TOOL_REGISTRY

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
        )
        iface_summaries = []
        for iface in interfaces:
            iface_summaries.append(
                f"- [{iface.get('interface_id')}] Type: {iface.get('type')} "
                f"File: `{iface.get('file_path', 'TBD')}` "
                f"Name: {iface.get('name', '')} "
                f"Desc: {iface.get('description', '')}"
            )

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}

### Convergence Summary
{convergence_summary}

### Parent Interfaces To Converge ({len(interfaces)} total)
{chr(10).join(iface_summaries)}

### Full Interface Definitions
```json
{json.dumps(interfaces, indent=2, ensure_ascii=False)}
```

This is a non-leaf convergence task.
Do only the minimal parent-level assembly needed to connect child capabilities into one coherent subsystem.
- First prefer a no-op outcome: if the current parent shell is already connected and builds successfully, make no code changes.
- Treat the provided child convergence summary as the source of truth for what child nodes already implemented and verified.
- Treat the provided `<frozen_node_contract>` as the stable parent assembly contract.
- Do NOT create new parent-layer implementation files in this phase.
- Prefer editing only existing parent-level shells, route containers, layout frames, shared provider composition points, and child mounting boundaries.
- If a target file already exists, read it first and use `edit_file` for minimal changes.
- Base your work on concrete child outputs: their implemented interfaces, landed files, and current pass/fail state.
- Do NOT re-implement child business logic in the parent.
- Do NOT create broad new feature code unless required for system connectivity.
- Do NOT introduce new auth semantics, fake user/session fallbacks, duplicate providers, or conflicting route ownership in the parent.
- Do NOT overwrite child feature behavior from the parent. Parent code may only mount, connect, guard, or expose child capabilities.
- If a child still has failing tests, avoid masking that failure in the parent. Only add the minimum parent wiring that remains valid.
- If repair is required, edit only the smallest set of existing shared files, then call `run_build` once.
- When the parent-level convergence is complete and the build passes, output exactly `IMPLEMENTED`.
"""
        system_content = self.get_system_prompt()
        if static_ctx:
            system_content = f"{system_content}\n\n{static_ctx}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]
        tools = [TOOL_REGISTRY[name]["schema"] for name in self._get_implement_tool_names() if name in TOOL_REGISTRY]
        previous_guard = self._non_leaf_existing_write_guard
        previous_no_write_guard = self._non_leaf_no_write_guard
        self._non_leaf_existing_write_guard = True
        self._non_leaf_no_write_guard = True
        try:
            result, messages = await self.run_from_messages(messages, node_id=node_id, max_steps=20, tools=tools)
        finally:
            self._non_leaf_existing_write_guard = previous_guard
            self._non_leaf_no_write_guard = previous_no_write_guard
        return result, messages

    async def audit_non_leaf_connectivity(
        self,
        node_id: str,
        interfaces: List[Dict[str, Any]],
        convergence_summary: str,
        preloaded_source: str = None,
    ) -> tuple[str, list]:
        from .context_pipeline import context_pipeline
        from .tools import TOOL_REGISTRY

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
        )
        iface_summaries = []
        for iface in interfaces:
            iface_summaries.append(
                f"- [{iface.get('interface_id')}] Type: {iface.get('type')} "
                f"File: `{iface.get('file_path', 'TBD')}` "
                f"Name: {iface.get('name', '')} "
                f"Desc: {iface.get('description', '')}"
            )

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}

### Convergence Summary
{convergence_summary}

### Parent Interfaces To Audit ({len(interfaces)} total)
{chr(10).join(iface_summaries)}

### Full Interface Definitions
```json
{json.dumps(interfaces, indent=2, ensure_ascii=False)}
```

This is a read-only non-leaf connectivity audit.
Your job is to inspect the current parent shell and child outputs, then decide whether parent-level code changes are actually necessary.
- Read the relevant existing shared shell files, routes, app entrypoints, providers, facades, and composition roots.
- Use the provided `<frozen_node_contract>` as the source of truth for allowed parent assembly scope.
- Verify whether the child capabilities are already mounted and connected coherently.
- Prefer a no-op result when the subsystem is already connected.
- Treat duplicate providers, fake auth/session fallbacks in parent shells, and parent-owned route conflicts as `CHANGES_REQUIRED`.
- Do NOT write or edit files in this audit.
- Do NOT invent new parent-layer implementation files.

Output exactly one of:
- `NO_CHANGES_NEEDED`
- `CHANGES_REQUIRED`
"""
        system_content = self.get_system_prompt()
        if static_ctx:
            system_content = f"{system_content}\n\n{static_ctx}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]
        tools = [TOOL_REGISTRY[name]["schema"] for name in self._get_non_leaf_audit_tool_names() if name in TOOL_REGISTRY]
        result, messages = await self.run_from_messages(messages, node_id=node_id, max_steps=12, tools=tools)
        return result, messages
