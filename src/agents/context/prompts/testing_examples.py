"""Shared runner examples for test generation and repair."""

VITEST_EXAMPLE = """
### Vitest imports and isolated database lifecycle (Web Unit/Integration only)
Use ESM imports in Vitest test files, even when backend application modules use
CommonJS. Do not convert the backend package to ESM or change package.json type.
NEVER use require('vitest'). NEVER obtain Vitest functions from globalThis;
the standard runner does not enable globals. Do not alternate these failed forms.
Use explicit imports instead of enabling globals as a workaround. Playwright E2E
files import test/expect from '@playwright/test', not from 'vitest'.
Playwright tests use relative navigation such as page.goto('/register'). The system
sets baseURL from the initialized --port; never hardcode localhost/127.0.0.1:3000
or override baseURL in generated tests. Reuse the supplied runtime target.

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
