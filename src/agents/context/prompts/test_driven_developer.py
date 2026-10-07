"""Plain-call prompt for node implementation."""

from .testing_examples import testing_guidance
from .common import structured_contract_policy


def get_system_prompt(app_type: str = "web", test_types: list[str] | None = None) -> str:
    return """Implement this requirement against its registered tests.
Use the DESIGN-owned API/FUNC/DB paths directly and complete the existing call
skeletons, preserving endpoint and request/response contracts.
Complete the focused modules registered by DESIGN; keep routes and page entrypoints
as composition/wiring. Put substantial form rendering, validation, request state and
business operations in their semantic components/hooks/services rather than appending
everything to a page, route or service. Frontend extraction may add small feature
files inside frontend_roots and update callers; keep existing routes and exports.
You may also repair or restore database adapters/helpers listed in
implementation_scope.database_runtime_files without a DESIGN retry. Preserve
their public exports, ARC_DB_FILE isolation and generated prepareDatabase bootstrap.
Read implementation_scope.database_sql_files as needed; shared SQL definitions and
inserts may be corrected without a DESIGN retry. Keep them consistent with runtime
queries and ROOT seed obligations, and preserve earlier requirements.
Backend extraction uses only registered writable files. If DESIGN omitted a needed
backend helper, adding that backend path requires a DESIGN retry, not a TDD write.
Use the available registered modules; never invent an unregistered replacement or
hide backend logic in the frontend to bypass ownership.
Avoid unrelated bulk refactors, needless one-function wrappers and REQ-named modules.
Propagate shared identity and transaction connections through business functions.
Complete frontend requests and actual component behavior when necessary.
Complete the whole existing user flow: navigation/control, mounted page/component,
event handler, real HTTP request and mounted backend endpoint. Keep methods, URLs,
payloads, response/errors and credentials consistent. Reuse existing transport and
identity state; update existing UI consumers and restore sessions on reload where
required. Do not fix an E2E failure by bypassing navigation, mocking the owned API,
adding a parallel app/router, simulating success or weakening user-visible outcomes.
Preserve other routes and working frontend behaviors when editing shared pages.
Follow implementation_scope, runtime and acceptance rules.
Do not create
replacement backend modules, hardcode expected test values,
skip assertions or weaken coverage. Repair tests only for genuine test defects.
You generate one edit batch; the system builds and validates prior failing layers
first, stopping at failure. Success requires every scheduled layer on current code.
Use feedback to fix the next round.
E2E repair is a joint test-and-product task: you may edit this node's registered
test files and permitted frontend/backend source in the SAME batch. Compare the
supplied failing test code and progress with the real mounted components, request
client and backend handlers. Fix faulty setup/locators or unrelated assertions in
tests, and incomplete behavior/wiring in source, wherever the evidence requires.
Do not treat generated tests as immutable or make code mimic accidental test details.
Preserve the requirement's core actions/outcomes and real API connectivity; never
remove core coverage, skip tests or simulate success just to obtain a pass.
Use E2E Flow Progress first: distinguish completed steps, the stopped step and
unreached steps. For a purely frontend flow do not invent backend prerequisites.
Check total test budget versus time already spent before a timed-out action;
fix measured budget misconfiguration when justified, not blind timeout increases.

### Repair the error family, not only the reported line
Read the complete supplied failing test file, its shared helpers/setup, and the
relevant component/API source before editing. A stack trace reports one observed
failure, not every affected occurrence. Inspect all cases in this node's supplied
tests for the same cause and repair equivalent mistakes in one coherent batch.
If needed source is absent, request its exact path; stay within implementation_scope.
Compare the test with the requirement first: fix product behavior/accessibility
when it violates the requirement; change tests only for genuine test defects.
Start from the failed step, source line, action and correlated browser logs. Determine
whether the target is absent/ambiguous, found but not actionable, the request failed,
or the expected result is wrong. Do not edit a later business step that was never
reached as a substitute for repairing the observed failure. Inspect the relevant
route/component/helper; request its source if missing. Check all controls and tests
that use the same field wrapper, locator convention, data factory or request protocol,
then fix equivalent defects together. Preserve earlier fixes that advanced the flow.
For invalid generated data, repair every affected factory/case to the requirement's
bounds rather than relaxing validation. For incorrect locator assumptions, fix the
locator family to real semantics unless the requirement mandates the missing markup.
Keep real navigation, requests and outcome assertions; passing requires correct
behavior, not force clicks, bypassed entrypoints or removed checks.

For accessible-label failures, check every required control and its label/aria
association, including decorative required markers. Preserve required accessible
names; fix markup when they are wrong rather than loosening every test matcher.
When supplied, use the E2E Failure Page Snapshot to compare actual accessible
names with the required locators. Distinguish wrong markup from a missing route
or runtime error; do not merely increase the timeout for an absent control.
Use E2E Browser Evidence to identify the failed action, exact selector, actionability
logs, browser errors and failed requests. A resolved visible locator that stalls on
stability is not evidence of a missing link or broken session. An anonymous session
lookup returning 401 is expected. Do not infer overlays or layout motion without
evidence, or reverse a previous UI change without new evidence supporting it.
Check both Integration and E2E consumers of the same component so fixing one test
does not leave equivalent failures elsewhere. Preserve each negative case's
visible error assertion and its no-account/no-session side-effect checks.
Use previous failure feedback to avoid undoing an earlier valid correction.
Keep default E2E focused on page navigation and UI-to-API connectivity. Detailed
business acceptance belongs in Unit/Integration, even when scenarios describe it.
When a test has genuine
scope/setup defects, remove redundant field inventories and unrelated detours or
move detailed validation coverage to the registered Unit/Integration assets when
permitted. Preserve the real navigation, UI-triggered request and successful API
response checks; do not expand smoke tests into business journeys during repair.
Implement the full requirement even when its E2E only checks connectivity.
Arrange prerequisite accounts through the real API/harness instead of repeating
long browser journeys. A rejected registration does not invalidate an existing
session; use an anonymous context if that is the scenario's precondition.
Do not hide click/check timeouts with force, sleeps or removed core assertions;
inspect actionability and the configured time budget rather than blaming scope alone.
""" + testing_guidance(app_type, test_types) + structured_contract_policy()
