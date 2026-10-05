"""Plain-call prompt for node implementation."""

from .testing_examples import testing_guidance


def get_system_prompt(app_type: str = "web", test_types: list[str] | None = None) -> str:
    return """Implement this requirement against its registered tests.
Use the DESIGN-owned API/FUNC/DB paths directly and complete the existing call
skeletons, preserving endpoint and request/response contracts.
Complete the focused modules registered by DESIGN; keep routes and page entrypoints
as composition/wiring. Put substantial form rendering, validation, request state and
business operations in their semantic components/hooks/services rather than appending
everything to a page, route or service. Frontend extraction may add small feature
files inside frontend_roots and update callers; keep existing routes and exports.
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
You generate one edit batch; the system builds and runs all scheduled test layers.
Use feedback to fix the next round.

### Repair the error family, not only the reported line
Read the complete supplied failing test file, its shared helpers/setup, and the
relevant component/API source before editing. A stack trace reports one observed
failure, not every affected occurrence. Inspect all cases in this node's supplied
tests for the same cause and repair equivalent mistakes in one coherent batch.
If needed source is absent, request its exact path; stay within implementation_scope.
Compare the test with the requirement first: fix product behavior/accessibility
when it violates the requirement; change tests only for genuine test defects.

For accessible-label failures, check every required control and its label/aria
association, including decorative required markers. Preserve required accessible
names; fix markup when they are wrong rather than loosening every test matcher.
Check both Integration and E2E consumers of the same component so fixing one test
does not leave equivalent failures elsewhere. Preserve each negative case's
visible error assertion and its no-account/no-session side-effect checks.
Use previous failure feedback to avoid undoing an earlier valid correction.
Keep E2E focused on the current requirement's core flow. When a test has genuine
scope/setup defects, remove redundant field inventories and unrelated detours or
move detailed validation coverage to the registered Unit/Integration assets when
permitted. Preserve the core real request, required result and explicit scenarios.
Arrange prerequisite accounts through the real API/harness instead of repeating
long browser journeys. A rejected registration does not invalidate an existing
session; use an anonymous context if that is the scenario's precondition.
Do not hide click/check timeouts with force, sleeps or removed core assertions;
inspect actionability and the configured time budget rather than blaming scope alone.
""" + testing_guidance(app_type, test_types)
