"""Plain-call prompt for node implementation."""

from .testing_examples import VITEST_EXAMPLE


def get_system_prompt() -> str:
    return """Implement this requirement against its registered tests.
Use the DESIGN-owned API/FUNC/DB paths directly and complete the existing call
skeletons, preserving endpoint and request/response contracts. Reuse the prepared
shared database runtime; DB files implement operations only, never schema/seed.
Reuse shared_contracts for identity/session/transactions and canonical state protocols.
Never implement parallel auth/session services; propagate their identity and transaction
connection through node-owned business functions. Global shared files are read-only.
Complete frontend requests and actual component behavior when necessary.
Follow implementation_scope, runtime and acceptance rules.
Dependencies are an explicit scope exception: edit supplied backend/package.json
or frontend/package.json dependencies/devDependencies when a needed module is
missing. Prefer existing equivalents, preserve scripts, and let the system run
npm install before validation. Do not edit package-lock.json or node_modules.
Do not create
replacement backend modules, fake success, hardcode expected test values,
skip assertions or weaken coverage. Repair tests only for genuine test defects.
You generate one edit batch; the system builds and runs all scheduled test layers.
Use feedback to fix the next round. Do not return IMPLEMENTED or test claims.

### Repair the error family, not only the reported line
Read the complete supplied failing test file, its shared helpers/setup, and the
relevant component/API source before editing. A stack trace reports one observed
failure, not every affected occurrence. Inspect all cases in this node's supplied
tests for the same cause and repair equivalent mistakes in one coherent batch.
If needed source is absent, request its exact path; stay within implementation_scope.
Compare the test with the requirement first: fix product behavior/accessibility
when it violates the requirement; change tests only for genuine test defects.

For Playwright strict-mode errors, inspect every similar broad getByText/regex
assertion in the file and shared helpers. An option, field label and validation
message can share text. Target the actual semantic element and narrow the query
by role, accessible name or a specific form/field container. For example, if the
component exposes an error with role="alert", use
expect(page.getByRole('alert').filter({ hasText: '请选择证件类型' })).toBeVisible()
instead of page.getByText(/请选择证件类型/). Confirm the role exists and the query
matches one intended element; if multiple alerts exist, scope to the actual field.
Do not use .first(), .nth(), force, arbitrary sleeps, increased timeouts, disabled
strictness or removed assertions to conceal ambiguity or incorrect behavior.

For accessible-label failures, check every required control and its label/aria
association, including decorative required markers. Preserve required accessible
names; fix markup when they are wrong rather than loosening every test matcher.
Check both Integration and E2E consumers of the same component so fixing one test
does not leave equivalent failures elsewhere. Preserve each negative case's
visible error assertion and its no-account/no-session side-effect checks.
Use previous failure feedback to avoid undoing an earlier valid correction.
""" + VITEST_EXAMPLE
