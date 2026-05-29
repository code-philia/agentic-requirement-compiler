import json
from typing import List, Dict, Any
from .arc_agent import ARCAgent

class TestDrivenDeveloper(ARCAgent):
    def __init__(self, broadcast_cb=None):
        super().__init__(
            agent_name="TestDrivenDeveloper",
            broadcast_cb=broadcast_cb
        )

    def get_system_prompt(self) -> str:
        from utils import get_app_type, get_android_package
        app_type = get_app_type()
        android_pkg = get_android_package() if app_type == "android" else ""

        if app_type == "android":
            tech_stack = f"""
### Strict Tech Stack Constraints:
**Android:**
- Language: Java 8
- Build System: Gradle 8.4 + AGP 8.1.4
- UI: XML Layout + AndroidX AppCompat + Material Components + ConstraintLayout
- Database: Room (SQLite)
- Testing:
  - All tests run on JVM via `./gradlew testDebugUnitTest` — STRICTLY JVM, no device/emulator
  - **Test directory structure** (sub-packages by type):
    ```
    app/src/test/java/{android_pkg.replace(".", "/")}/
      unit/           package {android_pkg}.unit;       Gradle: --tests "{android_pkg}.unit.*"
      integration/    package {android_pkg}.integration; Gradle: --tests "{android_pkg}.integration.*"
      e2e/            package {android_pkg}.e2e;         Gradle: --tests "{android_pkg}.e2e.*"
    ```
  - **Runner selection — two valid patterns only:**
    - **JUnit5** (no `@RunWith`) — pure-JVM unit tests where ALL Android deps are mocked with Mockito. Use `@ExtendWith(InstantTaskExecutorExtension.class)` for LiveData. Import it from `{android_pkg}.unit.InstantTaskExecutorExtension`.
    - **JUnit4 + `@RunWith(RobolectricTestRunner.class)` + `@Config(sdk = 31)`** — any test that needs Android Context, Activity, Room, or `AndroidViewModel`. Use `RuntimeEnvironment.getApplication()` for Context.
  - **NEVER use `@RunWith(AndroidJUnit4.class)`** — requires instrumentation
  - **NEVER import `androidx.test.core.app.ApplicationProvider`** — use `RuntimeEnvironment.getApplication()`
  - **NEVER import `androidx.test.core.app.ActivityScenario`** — use `Robolectric.buildActivity()`
  - **NEVER import `InstrumentationRegistry` or `androidx.test.espresso.*`**
  - **NEVER use `InstantTaskExecutorRule`** (`@Rule` is JUnit4) — use `@ExtendWith(InstantTaskExecutorExtension.class)`
  - **AndroidViewModel** needs a real Application — test with JUnit4+Robolectric or refactor to plain ViewModel + mock deps
  - **NEVER modify `app/build.gradle`** — all dependencies are pre-declared; if a class is missing, fix your import path, not the build file
  - **Package MUST match directory**: test files in `unit/` use `package {android_pkg}.unit;`, etc.
- Source directories: app/src/main/java/, app/src/test/java/{android_pkg.replace(".", "/")}/
- Package: {android_pkg}
"""
            pkg_compliance = f"""
### Package Compliance (CRITICAL for Android):
- The application package is `{android_pkg}`. You MUST use this package for ALL generated code:
  - `package {android_pkg};` in every main source Java file
  - `import {android_pkg}.xxx;` for cross-module references
  - File paths must use `{android_pkg.replace('.', '/')}/` as the package directory
  - AndroidManifest.xml must reference activities as `{android_pkg}.ActivityName
- For TEST code, use the correct sub-package matching the test type directory:
  - Unit tests: `package {android_pkg}.unit;`
  - Integration tests: `package {android_pkg}.integration;`
  - E2E tests: `package {android_pkg}.e2e;`
- Do NOT use `com.example.template` or any other package name.
- If the requirement description mentions a different package name in resource-id patterns, use THAT package name instead.

### How `run_tests` works:
- Call `run_tests(test_type="unit")` — the system automatically applies the correct Gradle `--tests` filter based on the test sub-package (e.g., `--tests "{android_pkg}.unit.*"`).
- You do NOT need to specify the filter yourself — just pass the `test_type`.
- The system executes tests in phases: batch run → individual retry → test downgrade. You only need to fix code and rerun when tests fail.
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
            pkg_compliance = ""

        return f"""You are an Elite Full-Stack Developer strictly following Test-Driven Development (TDD).
Your job is to implement the business logic for the provided interfaces until the corresponding tests pass.

Execution protocol (strict):
- Source code and test code are pre-injected in `<source_code>` and `<test_code>` — do NOT call `read_file` on files already provided in context.
- **First-pass strategy**: Study the pre-injected source code (existing stubs), test code (test expectations), and interface contracts (Inputs/Outputs/Callers/Callees). Implement ALL interfaces in a single batch of `write_file` calls, THEN call `run_tests` to verify.
- Write ALL implementation files FIRST, THEN run tests. Do NOT write one file and test immediately.
- If tests fail, read the error output carefully, use `edit_file` to fix the minimal set of issues (provide exact old_string/new_string), and rerun `run_tests`.
- If tests fail for environmental reasons, explicitly report the blocker and attempt a concrete fix.
- Return exactly "IMPLEMENTED" only when target tests are truly passing.

{tech_stack}

{pkg_compliance}
### Workflow:
1. Study the `<source_code>` (existing stubs) and `<test_code>` (test expectations) in context.
2. For each interface in "Current Interfaces to Implement", write the REAL implementation that satisfies its Outputs contract and makes the corresponding tests pass.
3. Use `write_file` to write ALL implementation files in one batch.
4. Call `run_tests` with the target test type to verify (Green phase).
5. If tests fail, read the error output, fix the code, and rerun `run_tests`.
6. If you need a new npm package, use `execute_command` (e.g., `npm install cors`).

Once `run_tests` returns a 100% passing state (Exit Code: 0) for the target tests, you MUST output exactly the word "IMPLEMENTED" in your final response to complete the task.
"""

    def get_tool_names(self) -> List[str]:
        return ["read_file", "write_file", "edit_file", "delete_file", "list_directory", "glob", "grep",
                "execute_command", "run_tests", "run_build", "search_interfaces_by_keyword", "search_interfaces_by_relation", "get_node_relations"]

    def build_initial_messages(self, node_id: str, test_files: List[str], test_type: str, req_desc: str, scenario: list = None, dependency_context: str = "", current_interfaces: list = None, preloaded_source: str = None) -> tuple:
        """Build the [system, user] messages and tools list without calling run().
        Returns (messages, tools) so the caller can use run_from_messages() or continue a session.
        """
        from .context_pipeline import context_pipeline

        # 1. Use the new Context Pipeline to build layered context for the TDD Agent
        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id, agent_type=self.agent_name, preloaded_source=preloaded_source
        )

        scenario_context = ""
        if test_type == "E2E" and scenario:
            scenario_context = f"\n### Target UI Scenario\n{json.dumps(scenario, indent=2, ensure_ascii=False)}"

        current_interfaces_str = "### Current Interfaces to Implement\n"
        if current_interfaces:
            for iface in current_interfaces:
                current_interfaces_str += f"- ID: {iface.get('interface_id')} (Type: {iface.get('type')})\n"
                if iface.get('file_path'):
                    current_interfaces_str += f"  File: `{iface.get('file_path')}`\n"
                if iface.get('first_line'):
                    current_interfaces_str += f"  Signature: `{iface.get('first_line')}`\n"
                # Extract full contract from content JSON
                try:
                    content = json.loads(iface.get('content', '{}'))
                    if content.get('description'):
                        current_interfaces_str += f"  Desc: {content['description']}\n"
                    if content.get('inputs'):
                        current_interfaces_str += f"  Inputs: {content['inputs']}\n"
                    if content.get('outputs'):
                        current_interfaces_str += f"  Outputs: {content['outputs']}\n"
                    if content.get('callers'):
                        current_interfaces_str += f"  Callers: {content['callers']}\n"
                    if content.get('callees'):
                        current_interfaces_str += f"  Callees: {content['callees']}\n"
                except:
                    pass
        else:
            current_interfaces_str += "No specific interface data provided."

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}

### Implementation Task for Node [{node_id}]
Target Test Type: {test_type}

### Requirement Description
{req_desc}
{scenario_context}

### Dependency Context
{dependency_context}

{current_interfaces_str}

### Target Test Files
{json.dumps(test_files, indent=2)}

**Implementation Strategy**:
1. Study the `<source_code>` (existing stubs) and `<test_code>` (test expectations) above.
2. For each interface in "Current Interfaces to Implement", write the REAL implementation that satisfies its Outputs contract and makes the corresponding tests pass.
3. Use `write_file` to write ALL implementation files in one batch.
4. Call `run_tests` with type `{test_type}` to verify.
5. If tests fail, use `edit_file` (exact old_string/new_string replacements) to fix and rerun. Reply "IMPLEMENTED" when all target tests pass.
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
        return messages, tools

    async def implement(self, node_id: str, test_files: List[str], test_type: str, req_desc: str, scenario: list = None, dependency_context: str = "", current_interfaces: list = None, preloaded_source: str = None) -> str:
        """Backwards-compatible: build initial messages then run a new session."""
        messages, tools = self.build_initial_messages(
            node_id=node_id, test_files=test_files, test_type=test_type,
            req_desc=req_desc, scenario=scenario, dependency_context=dependency_context,
            current_interfaces=current_interfaces, preloaded_source=preloaded_source
        )
        result, _ = await self.run_from_messages(messages, node_id=node_id, max_steps=15, tools=tools)
        return result
