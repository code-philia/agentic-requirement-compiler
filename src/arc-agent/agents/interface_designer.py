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
        from utils import get_app_type
        app_type = get_app_type()

        if app_type == "android":
            tech_stack = """
### Strict Tech Stack Constraints:
**Android:**
- Language: Java 8
- Build System: Gradle 8.4 + AGP 8.1.4
- UI: XML Layout + AndroidX AppCompat + Material Components + ConstraintLayout
- Database: Room (SQLite)
- Source directories: app/src/main/java/, app/src/test/java/
- Package: com.example.template
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

        return f"""You are a Principal Software Architect and Engineer.
Your task is to analyze a raw software requirement, design its interfaces (UI -> API -> FUNC -> DB), and implement them as concrete, executable STUB CODE in the real project directory.

For **non-leaf nodes**: implement ONLY the shared DB layer (Room Entity/DAO). Do NOT write Repository/ViewModel/Fragment/Layout — those belong to child nodes.

For **leaf nodes**: implement ALL layers with real logic (not just `throw UnsupportedOperationException`). Use actual DAO calls, return real data, wire up LiveData/queries.

{tech_stack}

Design constraints (strict):
- Prefer stable, deterministic module boundaries. One interface = one clear responsibility.
- Interface IDs must be stable and explicit: `IF_{{TYPE}}_{{DOMAIN}}_{{ACTION}}` (e.g., `IF_API_USER_LOGIN`).
- Keep contracts backward-compatible when reusing interfaces; use optional params for extensions.
- Do not invent dependency interfaces if they already exist in traceability search results.

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
   - For **leaf nodes**: Implement real logic (actual DAO calls, real data flow), NOT just `throw UnsupportedOperationException`. The code should be functional enough to pass basic tests.
   - For **non-leaf nodes**: Only DB layer stubs are needed (Entity/DAO).
4. **Check Compilation**: After implementing the interfaces, you MUST call the `run_build` tool to check for compilation errors. Use the build results log to fix any compilation or syntax errors before proceeding.

# Final Output Requirement:
After you have designed the architecture and written all the files, you MUST output a single JSON array in a markdown block (` ```json ... ``` `).
This JSON represents the Intermediate Representation (IR) mapping of the interfaces you just designed, implemented, or reused.
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
            "read_file", "write_file", "delete_file", "insert_lines", "replace_lines", "list_directory", "grep_search",
            "run_build", "search_interfaces_by_keyword", "search_interfaces_by_relation", "find_interface_impacts", "get_node_relations"
        ]

    async def design(self, node_id: str, requirement_data: dict, is_leaf: bool = True) -> str:
        from .context_pipeline import context_pipeline

        # 1. Use the new Context Pipeline to build layered context for the InterfaceDesigner
        context_str = context_pipeline.build_agent_context(node_id=node_id, agent_type=self.agent_name)

        # Build scope guidance based on node type
        if is_leaf:
            scope_guidance = """
### Node Scope: LEAF NODE (Full Implementation)
This is a **leaf node** (no children). You must design and implement ALL layers for this requirement:
- **DB layer**: Room entities, DAOs (only if not already created by a parent node)
- **API layer**: Repositories / Services
- **FUNC layer**: ViewModels / UseCases
- **UI layer**: Activities, Fragments, Adapters, XML layouts

Implement real logic in stubs (not just `throw UnsupportedOperationException`). Use the actual DAO/Repository calls, return real data from LiveData/queries.
"""
        else:
            scope_guidance = """
### Node Scope: NON-LEAF NODE (Shared Infrastructure Only)
This is a **non-leaf node** (it has children). Your job is **ONLY** to design and implement the **shared DB layer** that child nodes will depend on:
- **DB layer ONLY**: Room Entity classes, Room DAO interfaces, AppDatabase registration
- Do NOT create Repositories, ViewModels, Fragments, Adapters, or XML layouts — those belong to the child nodes.
- The DB layer you create here will be reused by all child nodes.

**CRITICAL**: Do not write UI, API, or FUNC layer code. Stop after the DB layer is complete and output your IR JSON.
"""

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{context_str}

### Current Target Requirement Node (ID: {node_id})
{json.dumps(requirement_data, indent=2, ensure_ascii=False)}

{scope_guidance}

Please perform the top-down decomposition for Node [{node_id}].
Then, generate the stub code files using the `write_file` tool.
Ensure your stubs import and use any required dependency interfaces from the context above.
When finished, output the mapping JSON block so the system can update the traceability database.
"""
        return await self.run(user_prompt=user_prompt, node_id=node_id, max_steps=15)
