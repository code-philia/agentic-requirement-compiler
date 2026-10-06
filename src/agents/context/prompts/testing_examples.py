"""Shared runner examples for test generation and repair."""

VITEST_EXAMPLE = """
### Vitest imports and isolated database lifecycle (Web Unit/Integration only)
Use ESM imports in Vitest test files, even when backend application modules use
CommonJS. Do not convert the backend package to ESM or change package.json type.
NEVER use require('vitest'). NEVER obtain Vitest functions from globalThis;
the standard runner does not enable globals. Do not alternate these failed forms.
Use explicit imports instead of enabling globals as a workaround.

Correct Vitest example:
import request from 'supertest';
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import app from '../src/app.js';
import database from '../src/database/index.js';

const { createTestDatabaseHarness } = database;
const harness = createTestDatabaseHarness({ label: 'register-api' });
let db;

beforeEach(async () => {
  db = await harness.setup();
});
afterEach(async () => {
  await harness.cleanup();
});

describe('registration persistence', () => {
  it('persists the successfully registered account', async () => {
    const payload = validRegistrationPayload();
    const response = await request(app).post('/api/register').send(payload);
    expect(response.status).toBe(201);
    const account = await db.get(
      'SELECT username FROM user_account WHERE username = ?', [payload.username],
    );
    expect(account.username).toBe(payload.username);
  });
});

This is a runner/lifecycle example, not a business contract. Define
validRegistrationPayload() from the actual requirement. Adapt imports, endpoint,
table/column names and expected status to supplied source and database contracts.
Keep real assertions and test isolation; do not copy sample names blindly.
"""

E2E_CONNECTIVITY_POLICY = """
### Web E2E scope: connectivity smoke tests
E2E checks wiring, not exhaustive business acceptance. Normally use one independent
short test per requirement; navigation-only and frontend-only requirements need no
invented API assertion. Reach a page through its existing link/control, assert the
destination URL and one stable mounted-page marker. If this requirement has an API
interaction, trigger it through the actual UI and await the matching real response.
Register waitForResponse BEFORE the triggering click/fill/navigation; match the
actual URL pathname AND HTTP method from source, not every request or an unrelated
session lookup. Assert response.ok() for a valid happy-path request. Do not accept
a mere outgoing request, 404/405/501, a failed response, or a mocked response as
proof of API connectivity. Do not assert detailed response bodies, database rows,
exact row counts/order, validation matrices, error wording, visual layout, control
inventories, or reload/session restoration in default E2E. Cover business rules in
Unit/Integration instead. Explicit test_intent can request deeper browser coverage.
Arrange authentication and prerequisite records with the real API/isolated harness
and actual browser credentials/cookies before visiting protected pages. Do not
assume a seeded account means the browser is logged in. Do not repeat prerequisite
registration/login UI journeys unless that journey is this requirement's subject.
Use actual, unique semantic locators with exact names where applicable. Do not
invent a form role/name from an outer section label. No force clicks or sleeps.
Example pattern (replace method/path/control with the actual source contract):
const responsePromise = page.waitForResponse(response =>
  new URL(response.url()).pathname === '/api/actual-endpoint' &&
  response.request().method() === 'POST');
await page.getByRole('button', { name: 'Submit', exact: true }).click();
const response = await responsePromise;
expect(response.ok()).toBe(true);
This verifies UI-to-API wiring; a standalone API request cannot replace that action.
"""

PLAYWRIGHT_EXAMPLE = """
### Playwright E2E runner conventions
import { test, expect } from '@playwright/test';
Use relative page.goto('/actual-route'); reuse the configured baseURL and runtime
port. Never hardcode an origin or override baseURL. Select unique semantic roles
and accessible names; scope validation errors to their actual alert/field container.
Never mask ambiguity with .first(), .nth(), force, sleeps or weaker assertions.
Keep browser tests to navigation and API connectivity; detailed business behavior
belongs in Unit/Integration. Use auto-waiting locators and
web-first assertions. Respect the configured test timeout: do not introduce shorter
per-test overrides or long setup chains.
"""


def testing_guidance(app_type: str, test_types: list[str] | None = None) -> str:
    """Initial generation has no chosen layers; concrete examples follow selection."""
    if app_type != "web":
        return ""
    layers = {str(kind).strip().lower().replace("_", "").replace("-", "") for kind in test_types or []}
    if not layers:
        return E2E_CONNECTIVITY_POLICY + "\nWeb runner conventions: Vitest uses explicit ESM imports for Unit/Integration; " \
            "Playwright E2E uses @playwright/test and relative navigation with configured baseURL. " \
            "Use the supplied isolated database harness for persistence tests.\n"
    parts = []
    if layers & {"unit", "integration", "unittest", "integrationtest"}:
        parts.append(VITEST_EXAMPLE)
    if layers & {"e2e", "endtoend"}:
        parts.append(PLAYWRIGHT_EXAMPLE)
    return E2E_CONNECTIVITY_POLICY + "\n".join(parts)
