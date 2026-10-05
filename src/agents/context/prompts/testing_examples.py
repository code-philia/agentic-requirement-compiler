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

PLAYWRIGHT_EXAMPLE = """
### Playwright E2E
import { test, expect } from '@playwright/test';
Use relative page.goto('/actual-route'); reuse the configured baseURL and runtime
port. Never hardcode an origin or override baseURL. Select unique semantic roles
and accessible names; scope validation errors to their actual alert/field container.
Never mask ambiguity with .first(), .nth(), force, sleeps or weaker assertions.
Keep browser tests to the minimal core interaction and required outcome; detailed
field/validation matrices belong in Unit/Integration. Use auto-waiting locators and
web-first assertions. Respect the configured test timeout: do not introduce shorter
per-test overrides or long setup chains. Required reload checks belong in the core
flow only when the requirement asks for persistence across reload.
"""


def testing_guidance(app_type: str, test_types: list[str] | None = None) -> str:
    """Initial generation has no chosen layers; concrete examples follow selection."""
    if app_type != "web":
        return ""
    layers = {str(kind).strip().lower().replace("_", "").replace("-", "") for kind in test_types or []}
    if not layers:
        return "\nWeb runner conventions: Vitest uses explicit ESM imports for Unit/Integration; " \
            "Playwright E2E uses @playwright/test and relative navigation with configured baseURL. " \
            "Use the supplied isolated database harness for persistence tests.\n"
    parts = []
    if layers & {"unit", "integration", "unittest", "integrationtest"}:
        parts.append(VITEST_EXAMPLE)
    if layers & {"e2e", "endtoend"}:
        parts.append(PLAYWRIGHT_EXAMPLE)
    return "\n".join(parts)
