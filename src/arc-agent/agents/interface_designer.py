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
        from utils import get_app_type, get_android_package
        app_type = get_app_type()

        if app_type == "android":
            android_pkg = get_android_package()
            tech_stack = f"""
### Strict Tech Stack Constraints:
**Android:**
- Language: Java 8
- Build System: Gradle 8.4 + AGP 8.1.4
- UI: XML Layout + AndroidX AppCompat + Material Components + ConstraintLayout
- Database: Room (SQLite)
- Source directories: app/src/main/java/, app/src/test/java/
- Package: {android_pkg}
- Interface decomposition: UI (Activity/Fragment) -> API (Repository/Service) -> FUNC (UseCase/ViewModel) -> DB (Room DAO/Entity)
"""
        else:
            tech_stack = """
### Strict Tech Stack Constraints:
**Frontend:**
- Framework: React 18+ (Vite)
- Language: JavaScript (ES6+)
- Styling: Tailwind CSS v4
- HTTP: Axios (MUST use Interceptors for global error handling in `src/api/axios.js`).

**Backend:**
- Runtime: Node.js (LTS)
- Framework: Express.js
- Database: SQLite3 (`sqlite3` driver, file-based)
"""

        return f"""You are a Principal Software Architect.
Your task is to analyze a raw software requirement and design its interfaces (UI -> API -> FUNC -> DB).

For **non-leaf nodes**: design ONLY the shared DB layer (Room Entity/DAO). Do NOT design Repository/ViewModel/Fragment/Layout — those belong to child nodes.

For **leaf nodes**: design ALL layers with real logic (not just `throw UnsupportedOperationException`). Use actual DAO calls, return real data, wire up LiveData/queries.

{tech_stack}

### Package Compliance (CRITICAL for Android):
- The application package is `{android_pkg}`. You MUST use this package for ALL generated code:
  - `package {android_pkg};` in every Java file
  - `import {android_pkg}.xxx;` for cross-module references
  - File paths must use `{android_pkg.replace('.', '/')}/` as the package directory
  - AndroidManifest.xml must reference activities as `{android_pkg}.ActivityName`
- Do NOT use `com.example.template` or any other package name.
- If the requirement description mentions a different package name in resource-id patterns (e.g., `org.billthefarmer.editor:id/newFile`), use THAT package name instead of `{android_pkg}`. The resource-id package takes priority.

Design constraints (strict):
- Prefer stable, deterministic module boundaries. One interface = one clear responsibility.
- Interface IDs must be stable and explicit: `IF_{{TYPE}}_{{DOMAIN}}_{{ACTION}}` (e.g., `IF_API_USER_LOGIN`).
- Keep contracts backward-compatible when reusing interfaces; use optional params for extensions.
- Do not invent dependency interfaces if they already exist in traceability search results.
- **UI Resource-ID Compliance**: If the requirement description or scenario specifies exact `resource-id` values (e.g., `org.billthefarmer.editor:id/newFile`), you MUST use those exact IDs when designing UI interfaces. The `android:id` in XML layouts and `findViewById(R.id.xxx)` in Java must match the resource-id suffix specified in the scenario. This is critical for automated testing to find the UI elements.

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
            "read_file", "list_directory", "grep_search",
            "search_interfaces_by_keyword", "search_interfaces_by_relation",
            "find_interface_impacts", "get_node_relations"
        ]

    def _get_implement_tool_names(self) -> List[str]:
        return [
            "read_file", "write_file", "delete_file", "insert_lines", "replace_lines",
            "list_directory", "grep_search", "run_build",
            "search_interfaces_by_keyword", "search_interfaces_by_relation",
            "find_interface_impacts", "get_node_relations"
        ]

    async def design_ir(self, node_id: str, requirement_data: dict, is_leaf: bool = True) -> str:
        """Phase 1: Design interfaces and output IR JSON. No code writing."""
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
### Node Scope: NON-LEAF NODE (Shared Infrastructure Only)
This is a **non-leaf node** (it has children). Design **ONLY** the **shared DB layer**:
- **DB layer ONLY**: Room Entity classes, Room DAO interfaces, AppDatabase registration
- Do NOT design Repositories, ViewModels, Fragments, Adapters, or XML layouts — those belong to child nodes.

**CRITICAL**: Do not design UI, API, or FUNC layer interfaces. Stop after the DB layer and output your IR JSON.
"""

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}

### Current Target Requirement Node (ID: {node_id})
{json.dumps(requirement_data, indent=2, ensure_ascii=False)}

{scope_guidance}

Perform the top-down decomposition for Node [{node_id}].
Design the interface architecture and output the IR JSON mapping.
Do NOT write any code files — this phase is ONLY for architecture design.
"""
        return await self.run(user_prompt=user_prompt, node_id=node_id, max_steps=10, static_context=static_ctx)

    async def implement_stubs(self, node_id: str, interfaces: List[Dict], is_leaf: bool = True) -> str:
        """Phase 2: Implement stub code for each interface. Track progress per interface."""
        from .context_pipeline import context_pipeline
        from utils import get_app_type, get_android_package

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(node_id=node_id, agent_type=self.agent_name)

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
**UI Resource-ID Compliance**: When writing XML layouts, use the exact `android:id` values specified in the requirement description/scenario (e.g., if scenario says `org.billthefarmer.editor:id/newFile`, the XML must have `android:id="@+id/newFile"`). When writing Java code, use `findViewById(R.id.newFile)` with matching IDs.
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

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}

### Implementation Task for Node [{node_id}]
{impl_guidance}

### Interfaces to Implement ({len(interfaces)} total):
{chr(10).join(iface_summaries)}

### Full Interface Definitions:
```json
{json.dumps(interfaces, indent=2, ensure_ascii=False)}
```

Write ALL stub code files using `write_file` calls FIRST, then call `run_build` ONCE to verify compilation.
Do NOT call `read_file` on source files — they are already provided in the `<source_code>` context above.
Do NOT interleave `read_file` and `write_file` — batch all writes together.
Ensure all imports, class hierarchies, and method signatures match the interface definitions above.
Fix any build errors found.
When all files are written and compilation passes, output "IMPLEMENTED".
"""
        # Override tool names for implementation phase
        original_tool_names = self.get_tool_names
        self.get_tool_names = lambda: self._get_implement_tool_names()
        try:
            result = await self.run(user_prompt=user_prompt, node_id=node_id, max_steps=20, static_context=static_ctx)
        finally:
            self.get_tool_names = original_tool_names
        return result
