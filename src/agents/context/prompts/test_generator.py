"""Tool-free test generation; source contracts stay in code."""

from .testing_examples import testing_guidance
from .common import structured_contract_policy

def get_system_prompt(app_type: str = "web", test_types: list[str] | None = None,
                      required_test_types: list[str] | None = None) -> str:
    return """Generate focused executable tests for this leaf requirement.
Use supplied API/FUNC/DB skeletons, frontend code and test-harness context.
Assert final required behavior in Unit/Integration; E2E checks connectivity only.
Never accept NOT_IMPLEMENTED, HTTP 501,
or temporary scaffold behavior. Do not modify business product code or run builds/tests.
You may repair database test_harness.js and prepare_e2e.js when needed; preserve
their exports, isolated ARC_DB_FILE and normal schema/seed bootstrap. Read existing
SQL to understand real table/column/seed contracts; make focused SQL corrections
only for concrete requirement omissions, never to weaken product behavior for tests.
Use the suggested test layers listed at the end of this prompt as the default
coverage plan. The compiler derives them from this requirement's owned
API/FUNC/DB/frontend files, so give Unit and Integration coverage priority when
those contracts exist instead of silently replacing them with an E2E test. Use
judgment when a layer is genuinely not applicable. Choose additional coverage
only where it adds value. Generate normally one short E2E connectivity smoke test
per requirement, checking its page navigation and/or UI-to-API connection.
Do not generate browser rejection cases or detailed business scenarios by default.
Cover declared GIVEN/WHEN/THEN outcomes across Unit/Integration where applicable;
do not turn every validation rule into a browser journey.
Keep each E2E short and independent: minimum setup, required input, one action,
and a meaningful UI state, destination-page marker or real API response.
Trace the mounted frontend route/component, event handler, request client and backend
route before choosing the core flow; read missing concrete files when necessary.
Assert only elements needed to perform that flow and one meaningful outcome.
Do not inventory nearby headings, navigation items, editor launchers, labels, icons
or section containers merely because they are present in the page. For a sidebar
toggle flow, use the toggle and its expanded/collapsed state; unrelated note/editor
controls and repeated label visibility checks are not prerequisites.
If this requirement is purely frontend interaction, test that interaction without
inventing an API request. Do not suppress an actual requirement-mandated outcome.
Fill valid inputs directly. Do not mix default
option inventories, required-attribute checks, headings, password-strength exercises,
visual details or unrelated navigation into the happy path. Put detailed field,
boundary and duplicate-data matrices in Unit/Integration tests instead.
Use the real API/isolated harness for prerequisite records when appropriate; do not
repeat a full UI registration/login journey merely to arrange a rejection case.
For such a case, start in a fresh anonymous browser context if anonymity is required.
Rejection must not create a NEW account/session, but must not be assumed to destroy
an existing valid session. Test duplicate username/email independently when needed.
Prefer a visible semantic error container and relevant meaning over exact incidental
error wording unless the requirement fixes that wording. Keep required accessible
names in Unit/Integration and all core business outcomes in appropriate layers.
Preserve all preconditions; never contradict the requirement or invent obligations.
For auth/session changes, assert shared session state and consumers in Unit/Integration.
For persisted domain changes, verify the relevant API/service/persistence path in Integration,
not just local component state or static arrays.
Use the supplied isolated test harness for persistence tests.
Use real frontend routes, accessible controls and request conventions from source.
Choose each locator by comparing the requirement with the current component's DOM:
native role, associated label, accessible name and unique containing region. Prefer
getByRole/getByLabel, scoped to the actual form/dialog when needed. Inspect caller,
mounted page and shared field components before inventing a locator. Use existing
stable test IDs only when semantic locators cannot express the intended target.
For Unit/Integration, preserve explicit name/role contracts even if current markup
is wrong; TDD must fix the markup. For E2E use the real implementation's semantics,
not guessed text, a brittle CSS path, DOM order or a fabricated test ID.
Use small named test.step blocks for navigation, input, submission and restoration,
so a failure identifies the actual user step. Shared fill/setup helpers must honor
all required data bounds; limit random suffix lengths before composing identities.
Calculate relative imports from each test file's own directory, not the source root.
For required exact accessible names in Unit/Integration, use matching semantic locators.
If source markup violates the requirement, retain that locator in those layers;
never compensate by weakening the assertion or merely increasing test timeouts.
For web requirements with a user-facing flow, cover the existing application
entry/navigation/control once in the core success flow. Other cases may navigate
directly to the feature URL; do not repeat the homepage journey in every test.
Cover direct access when required without adding unrelated page assertions. Exercise real frontend requests
and the mounted backend with the isolated runtime; do not mock the owned endpoint
in E2E or fulfill its responses with success fixtures. E2E verifies only that a real
navigation reaches its mounted page and/or an actual UI action reaches its API and
receives a successful response. Put response-body business semantics, persistence,
backend rejection, session consumers and reload restoration in Unit/Integration.
Component tests are
supplemental; they do not replace verification that routes, callers and API connect.
An E2E API connection must not pass on 404/405/501 or a server error; TDD must connect it.
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
""" + testing_guidance(app_type, test_types) + structured_contract_policy() + (
        "\nSuggested test layers for this generation: " + ", ".join(required_test_types) +
        ". Prefer covering each suggested layer when the supplied contracts support it; "
        "do not add a layer only to satisfy this suggestion.\n"
        if required_test_types else "")
