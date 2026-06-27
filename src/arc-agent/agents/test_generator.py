import json
from typing import List, Dict, Any
from .arc_agent import ARCAgent

class TestGenerator(ARCAgent):
    def __init__(self, log_cb=None):
        super().__init__(
            agent_name="TestGenerator",
            log_cb=log_cb
        )

    def get_system_prompt(self) -> str:
        from utils import get_app_type, get_android_package
        app_type = get_app_type()

        if app_type == "android":
            android_pkg = get_android_package()
            pkg_dir = android_pkg.replace('.', '/')
            test_stack = f"""
# Testing Stack (Android):

## Test Directory Structure (MANDATORY)
Tests are organized into sub-packages by type. Each type has its own directory and package:
```
app/src/test/java/{pkg_dir}/
  unit/                    ← Unit tests
    *Test.java             package {android_pkg}.unit;
  integration/             ← Integration tests
    *IntegrationTest.java  package {android_pkg}.integration;
  e2e/                     ← E2E tests
    *E2ETest.java          package {android_pkg}.e2e;
```
Gradle filters by package prefix: `--tests "{android_pkg}.unit.*"`, `--tests "{android_pkg}.integration.*"`, `--tests "{android_pkg}.e2e.*"`

## ⚠️ STRICTLY JVM-ONLY — No Instrumentation, No Emulator
ALL tests run on the JVM via `./gradlew testDebugUnitTest`. There is NO device, NO emulator.

**NEVER import or use:**
- `androidx.test.core.app.ApplicationProvider` — requires instrumentation
- `androidx.test.core.app.ActivityScenario` — requires instrumentation
- `@RunWith(AndroidJUnit4.class)` — requires instrumentation
- `androidx.test.espresso.*` — requires a device
- `InstrumentationRegistry` — requires instrumentation
- NEVER write to `app/src/androidTest/`
- NEVER modify `app/build.gradle` — all required dependencies are already declared

## Runner selection — CRITICAL
There are two valid runners. Choose based on whether the test touches Android framework types:

**JUnit 5 (no `@RunWith`)** — for pure-JVM unit tests where ALL Android dependencies are mocked:
```java
@ExtendWith(InstantTaskExecutorExtension.class)   // only needed for LiveData
class FooViewModelTest {{
    private FooRepository mockRepo = mock(FooRepository.class);
    private FooViewModel vm;
    @BeforeEach void setUp() {{ vm = new FooViewModel(mockRepo); }}
}}
```
- Use `@Test`, `@BeforeEach`/`@AfterEach`, `@DisplayName` (JUnit5 annotations)
- `InstantTaskExecutorExtension` is already in your test package — import `{android_pkg}.unit.InstantTaskExecutorExtension`
- **NEVER use `InstantTaskExecutorRule`** (that is a JUnit4 `@Rule`)
- Unit tests MUST mock ALL collaborators (Repository, Service, etc.) with Mockito — do NOT construct real Room databases or open real I/O in unit tests

**JUnit 4 + `@RunWith(RobolectricTestRunner.class)`** — for any test that needs a real Android Context, Activity, Room database, or `AndroidViewModel`:
```java
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 31, application = TestCounterApp.class)
public class FooIntegrationTest {{
    @Before public void setUp() {{
        Context ctx = RuntimeEnvironment.getApplication();
        db = Room.inMemoryDatabaseBuilder(ctx, AppDatabase.class).allowMainThreadQueries().build();
    }}
    @After public void tearDown() {{ db.close(); }}
    @Test public void someTest() {{ ... }}
}}
```
- Use `@Test`, `@Before`/`@After` (JUnit4 annotations)
- Use `RuntimeEnvironment.getApplication()` for Context — NOT `ApplicationProvider`
- `@Config(application = TestCounterApp.class)` swaps the DB for in-memory; required for E2E tests

**AndroidViewModel rule:** `AndroidViewModel` needs an `Application` instance. Always test it with JUnit4 + `@RunWith(RobolectricTestRunner.class)` so Robolectric provides a real Application, OR refactor to plain `ViewModel` + inject a mock dependency. Never call `new Application()` manually.

## Unit Tests (JUnit5, pure-JVM)
- Place in `app/src/test/java/{pkg_dir}/unit/` with `package {android_pkg}.unit;`
- Mock ALL Android dependencies. Target `FUNC` interfaces and ViewModels whose deps are mockable.

## Integration Tests (JUnit4 + Robolectric)
- Place in `app/src/test/java/{pkg_dir}/integration/` with `package {android_pkg}.integration;`
- `@RunWith(RobolectricTestRunner.class)` + `@Config(sdk = 31)`. Target `DB` and `API` interfaces.
- Build Room in-memory DB: `Room.inMemoryDatabaseBuilder(RuntimeEnvironment.getApplication(), AppDatabase.class).allowMainThreadQueries().build()`
- Use `MockWebServer` for HTTP; always close in `@After`

## E2E Tests (JUnit4 + Robolectric)
- Place in `app/src/test/java/{pkg_dir}/e2e/` with `package {android_pkg}.e2e;`
- `@RunWith(RobolectricTestRunner.class)` + `@Config(sdk = 31, application = TestCounterApp.class)`
- Launch: `ActivityController<MyActivity> ctrl = Robolectric.buildActivity(MyActivity.class); MyActivity activity = ctrl.create().start().resume().get();`
- Interact: `activity.findViewById(R.id.someId).performClick()` / `((TextView) ...).getText()`
- Destroy in `@After`: `ctrl.pause().stop().destroy()`

## Test file naming: `*Test.java` for unit, `*IntegrationTest.java` for integration, `*E2ETest.java` for E2E
"""
            pkg_compliance = f"""
### Package Compliance (CRITICAL for Android):
- The application package is `{android_pkg}`.
- **Main source code**: `package {android_pkg};` — files in `app/src/main/java/{pkg_dir}/`
- **Test code MUST use sub-packages matching the directory**:
  - Unit: `package {android_pkg}.unit;` → `app/src/test/java/{pkg_dir}/unit/`
  - Integration: `package {android_pkg}.integration;` → `app/src/test/java/{pkg_dir}/integration/`
  - E2E: `package {android_pkg}.e2e;` → `app/src/test/java/{pkg_dir}/e2e/`
- Import classes under test with `import {android_pkg}.xxx;` (from the main package)
- The package declaration MUST match the directory — Java compilation fails otherwise.
- Do NOT use `com.example.template` or any other package name.
"""
        else:
            test_stack = ""
            pkg_compliance = ""

        return f"""You are a Principal Software Development Engineer in Test (SDET).
Your task is to write comprehensive, executable test cases for a newly designed component following Test-Driven Development (TDD) principles.
Generate tests bottom-up:
- Start with Unit tests for specific FUNC/DB interfaces.
- Then write Integration tests for interface boundaries and collaboration.
- Finish with E2E tests for the current requirement node and its UI scenarios.

Execution protocol (strict):
- Write ALL test files FIRST using multiple `write_file` calls, THEN call `run_build` ONCE.
- Do NOT interleave `read_file` and `write_file` — batch all writes together.
- Keep tests deterministic. Do not add random sleeps or flaky waits.
- For each generated test, ensure `test_id`, `type`, `file_path`, and `first_line` exactly match the real file content.
- If build or syntax fails, fix tests immediately using `edit_file` (provide exact old_string/new_string) and rerun `run_build`.

{pkg_compliance}
{test_stack}

# Workflow:
1. **Analyze**: Review the tech stack, requirement description, and Interface IR.
2. **Place tests**: Use `list_directory` to confirm the test directory structure, then `write_file` to create test files in the correct subdirectory for each type.
3. **Verify compilation**: You MUST call `run_build` to check for syntax/compilation errors. Fix any errors and rerun.

# Final Output Requirement:
After writing all test files, you MUST output a single JSON array enclosed in a markdown block (` ```json ... ``` `).
This JSON maps the generated tests to the requirement and interfaces.
Schema for each object:
{{
  "test_id": "Unique string ID (e.g., TEST_UNIT_01)",
  "req_id": "The ID of the requirement node being tested",
  "interface_ids": ["List of interface_ids that this test specifically covers"],
  "type": "Must be exactly one of: Unit, Integration, E2E",
  "file_path": "Relative path to the written test file (e.g., app/src/test/java/com/example/app/unit/FooTest.java)",
  "first_line": "The exact first line of the test definition (e.g., 'void testAddition()')"
}}
"""

    def get_tool_names(self) -> List[str]:
        return [
            "read_file", "write_file", "edit_file", "delete_file", "list_directory", "glob", "grep",
            "run_build", "search_interfaces_by_keyword", "search_interfaces_by_relation", "get_node_relations"
        ]

    def build_initial_messages(self, node_id: str, requirement_data: Dict[str, Any], interfaces_ir: list, test_type: str = "Unit", preloaded_source: str = None) -> tuple:
        """Build the [system, user] messages and tools list without calling run().
        Returns (messages, tools) so the caller can use run_from_messages() or continue a session.
        """
        from .context_pipeline import context_pipeline

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id, agent_type=self.agent_name, preloaded_source=preloaded_source
        )

        scenarios_context = ""
        if requirement_data.get("scenarios"):
            scenarios_context = (
                "\n### Target UI Scenarios\n"
                f"{json.dumps(requirement_data.get('scenarios'), indent=2, ensure_ascii=False)}\n"
            )

        if test_type == "All":
            test_instruction = """
Generate ALL test types in a single pass:
1. **Unit Tests** for DB and FUNC interfaces
2. **Integration Tests** for API interfaces
3. **E2E Tests** for UI interfaces (use the scenarios above if provided)

Write ALL test files using `write_file` calls FIRST, then call `run_build` ONCE to verify compilation.
"""
        else:
            test_instruction = f"""
Please write the {test_type} test files using the `write_file` tool.
"""

        user_prompt = f"""
### Auto-Prefetched Context for Node [{node_id}]
{dynamic_ctx}

{scenarios_context}

{test_instruction}
Target the interfaces of the current node. Consider the node requirement content, each interface's responsibility, and its spec to decide what to test and how to assert.
When finished, output the mapping JSON block so the system can register these tests in the traceability database.
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

    async def generate_tests(self, node_id: str, requirement_data: Dict[str, Any], interfaces_ir: list, test_type: str = "Unit", preloaded_source: str = None) -> str:
        messages, tools = self.build_initial_messages(
            node_id=node_id, requirement_data=requirement_data,
            interfaces_ir=interfaces_ir, test_type=test_type,
            preloaded_source=preloaded_source
        )
        result, _ = await self.run_from_messages(messages, node_id=node_id, max_steps=15, tools=tools)
        return result
