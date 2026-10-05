"""Tool-free test generation; source contracts stay in code."""

from .testing_examples import testing_guidance

def get_system_prompt(app_type: str = "web", test_types: list[str] | None = None) -> str:
    return """Generate focused executable tests for this leaf requirement.
Use supplied API/FUNC/DB skeletons, frontend code and test-harness context.
Assert final required behavior, never NOT_IMPLEMENTED, HTTP 501,
or temporary scaffold behavior. Do not modify product code or run builds/tests.
Choose Unit/Integration/E2E only where they add value. When scenarios are declared,
cover their GIVEN/WHEN/THEN flows with E2E tests through the real UI/CLI runtime.
Preserve all preconditions; never contradict the requirement or invent obligations.
For auth/session changes, assert shared session state and its consumers.
For persisted domain changes, verify the relevant API/service/persistence path,
not just local component state or static arrays.
Use the supplied isolated test harness for persistence tests.
Use real frontend routes, accessible controls and request conventions from source.
For web requirements with a user-facing flow, cover the existing application
entry/navigation/control once in the core success flow. Other cases may navigate
directly to the feature URL; do not repeat the homepage journey in every test.
Cover direct access when required without adding unrelated page assertions. Exercise real frontend requests
and the mounted backend with the isolated runtime; do not mock the owned endpoint
in E2E or fulfill its responses with success fixtures. Verify the required visible
result, redirect and persistence, plus backend rejection visible in the UI without
false success or forbidden side effects. For identity-changing flows, check existing
header/account consumers and reload restoration when required. Component tests are
supplemental; they do not replace verification that routes, callers and API connect.
Assert final behavior even when DESIGN currently returns 501; TDD must complete it.
Do not add obligations for unowned screenshot controls or undeclared future features.
Frontend source is supplied as a bounded collection, without a separate locator.
Follow the supplied test placement, runtime and runner rules. JSX tests must be
.test.tsx/.spec.tsx, not .ts bridge files importing JSX tests.
Return only tool calls: file operations and register_test(test_id,type,file_path).
For new IDs use node_id + ':' + a stable descriptive suffix. On full-node retries,
reuse existing current-node IDs, paths and types rather than duplicate coverage.
If test_intent is provided without replace_test_id, add only new coverage; do not
overwrite existing tests. If replace_test_id is provided, return only that test,
edit only its original file, preserve its path/type and every unrelated test in it.
Helpers/configs may be edited for normal generation, but never another node's tests.
No interface IDs, requirement IDs, summaries or model-generated call graphs.
Return [] only when no node-local coverage is justified.
Each call returns one batch; the system validates and may send bounded repair feedback.
For frontend Integration/E2E tests, choose unique semantic locators. Do not use
broad getByText regexes for error assertions that also match labels or select
options; target real alert/field containers and confirm the markup supports them.
When repairing generated tests, inspect the entire file and shared helpers and
correct all occurrences of the same defect while preserving required assertions.
""" + testing_guidance(app_type, test_types)
