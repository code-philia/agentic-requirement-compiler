import asyncio
import json
import socket
import time
from pathlib import Path

import pytest

from app_type_handler import web
from app_type_handler.test_results import parse_test_results


def test_grouped_e2e_database_prepare_failure_has_top_level_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    handler = web.WebAppType(str(tmp_path), "", None, lambda *_: None)
    monkeypatch.setattr(web, "_build_frontend_dist", lambda *_: _async_result((True, "Exit Code: 0")))
    monkeypatch.setattr(
        web,
        "_prepare_e2e_database",
        lambda *_: _async_result((False, "Command timed out after 60.0 seconds.")),
    )

    output = asyncio.run(
        handler.run_test_group("E2E", ["backend/test-e2e/req-1.e2e.test.ts"])
    )

    assert "E2E database preparation failed" in output
    assert "System-Derived Commands:" in output
    assert "backend: npx playwright test test-e2e/req-1.e2e.test.ts" in output
    assert parse_test_results(output)["exit_code"] == 1


def test_e2e_quality_gate_rejects_isolated_request_cookie_assertion(tmp_path: Path):
    test_path = tmp_path / "backend" / "test-e2e" / "auth.e2e.test.ts"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "import { expect, test } from '@playwright/test';\n"
        "test('login', async ({ page, request }) => {\n"
        "  await page.goto('/login');\n"
        "  const response = await request.get('/api/auth/session');\n"
        "  expect(response.ok()).toBeTruthy();\n"
        "});\n",
        encoding="utf-8",
    )
    handler = web.WebAppType(str(tmp_path), "", None, lambda *_: None)

    error = handler.validate_test_content("E2E", "backend/test-e2e/auth.e2e.test.ts")

    assert error is not None
    assert "does not share the page cookie jar" in error


def test_e2e_quality_gate_rejects_unscoped_broad_text_regex(tmp_path: Path):
    test_path = tmp_path / "backend" / "test-e2e" / "errors.e2e.test.ts"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "import { expect, test } from '@playwright/test';\n"
        "test('error', async ({ page }) => {\n"
        "  await page.goto('/register');\n"
        "  await expect(page.getByText(/required|email|password/i)).toBeVisible();\n"
        "});\n",
        encoding="utf-8",
    )
    handler = web.WebAppType(str(tmp_path), "", None, lambda *_: None)

    error = handler.validate_test_content("E2E", "backend/test-e2e/errors.e2e.test.ts")

    assert error is not None
    assert "unscoped broad getByText regex" in error


def test_e2e_quality_gate_accepts_cookie_aware_scoped_assertions(tmp_path: Path):
    test_path = tmp_path / "backend" / "test-e2e" / "valid.e2e.test.ts"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "import { expect, test } from '@playwright/test';\n"
        "test('login', async ({ page }) => {\n"
        "  await page.goto('/login');\n"
        "  const response = await page.context().request.get('/api/auth/session');\n"
        "  expect(response.ok()).toBeTruthy();\n"
        "  await expect(page.getByRole('alert')).toContainText(/invalid credentials/i);\n"
        "});\n",
        encoding="utf-8",
    )
    handler = web.WebAppType(str(tmp_path), "", None, lambda *_: None)

    assert handler.validate_test_content("E2E", "backend/test-e2e/valid.e2e.test.ts") is None


@pytest.mark.parametrize(
    "lifecycle_code",
    [
        (
            "import harness from '../src/database/test_harness';\n"
            "const runtimeHarness = harness.createRuntimeDatabaseHarness({ "
            "dbPath: process.env.ARC_DB_FILE });\n"
            "test.beforeEach(async () => runtimeHarness.reset());\n"
        ),
        "import sqlite3 from 'sqlite3';\n",
        "const databasePath = process.env.ARC_E2E_DB_PATH;\n",
    ],
)
def test_e2e_quality_gate_rejects_spec_owned_database_lifecycle(
    tmp_path: Path,
    lifecycle_code: str,
):
    test_path = tmp_path / "backend" / "test-e2e" / "database-owner.e2e.test.ts"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "import { expect, test } from '@playwright/test';\n"
        + lifecycle_code
        + "test('uses the running app', async ({ page }) => {\n"
        "  await page.goto('/');\n"
        "  await expect(page.getByRole('main')).toBeVisible();\n"
        "});\n",
        encoding="utf-8",
    )
    handler = web.WebAppType(str(tmp_path), "", None, lambda *_: None)

    error = handler.validate_test_content("E2E", "backend/test-e2e/database-owner.e2e.test.ts")

    assert error is not None
    assert "ARC already prepares an isolated database" in error


def test_e2e_runtime_contract_separates_vitest_and_playwright_database_ownership():
    contract = "\n".join(web.WebAppType.test_harness_lines())
    stack = web.WebAppType.build_stack_block()

    assert "Unit/Integration database tests may use" in contract
    assert "ARC owns the Playwright E2E lifecycle" in contract
    assert "E2E spec files must not import or invoke" in contract
    assert "Vitest Unit/Integration database tests create" in stack
    assert "Playwright E2E is runner-owned" in stack


def test_vitest_quality_gate_rejects_commonjs_vitest_import(tmp_path: Path):
    test_path = tmp_path / "backend" / "tests" / "auth" / "routes.integration.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "const { describe, expect, it } = require('vitest');\n"
        "describe('auth', () => {\n"
        "  it('responds', () => expect(200).toBe(200));\n"
        "});\n",
        encoding="utf-8",
    )
    handler = web.WebAppType(str(tmp_path), "", None, lambda *_: None)

    error = handler.validate_test_content(
        "Integration",
        "backend/tests/auth/routes.integration.test.js",
    )

    assert error is not None
    assert "fails before tests load" in error
    assert "createRequire(import.meta.url)" in error


def test_test_quality_gate_rejects_degenerate_identical_conditional(tmp_path: Path):
    test_path = tmp_path / "backend" / "tests" / "schema.unit.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "import { expect, it } from 'vitest';\n"
        "it('creates schema', async () => {\n"
        "  await ensureSchema({ exec: harness.setup ? undefined : undefined });\n"
        "  expect(true).toBeTruthy();\n"
        "});\n",
        encoding="utf-8",
    )
    handler = web.WebAppType(str(tmp_path), "", None, lambda *_: None)

    error = handler.validate_test_content("Unit", "backend/tests/schema.unit.test.js")

    assert error is not None
    assert "degenerate conditional" in error
    assert "real collaborator/fixture value" in error


def test_unit_quality_gate_rejects_callback_that_reenters_harness(tmp_path: Path):
    test_path = tmp_path / "backend" / "tests" / "schema.unit.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "import { expect, it } from 'vitest';\n"
        "it('creates schema', async () => {\n"
        "  const calls = [];\n"
        "  const exec = async (sql) => { calls.push(sql); await harness.reset(); };\n"
        "  await ensureSchema({ exec });\n"
        "  expect(calls).toHaveLength(2);\n"
        "});\n",
        encoding="utf-8",
    )
    handler = web.WebAppType(str(tmp_path), "", None, lambda *_: None)

    error = handler.validate_test_content("Unit", "backend/tests/schema.unit.test.js")

    assert error is not None
    assert "circular verification" in error
    assert "only record" in error


def test_backend_startup_surfaces_early_syntax_crash_without_waiting_for_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "package.json").write_text(
        json.dumps({"scripts": {"start": "node crash.js"}}),
        encoding="utf-8",
    )
    (backend / "crash.js").write_text(
        "console.error('SyntaxError: Duplicate export at backend/src/services/auth_service.js:116');\n"
        "process.exit(1);\n",
        encoding="utf-8",
    )
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    monkeypatch.setattr(web, "get_web_port", lambda: port)

    started = time.perf_counter()
    process, command, detail, fingerprint = asyncio.run(
        web._start_backend_runtime(str(tmp_path), {})
    )
    elapsed = time.perf_counter() - started

    assert process is None
    assert command == "npm run start"
    assert fingerprint == ""
    assert elapsed < 5
    assert "exited before listening with exit code 1" in detail
    assert "SyntaxError: Duplicate export" in detail
    assert "backend/src/services/auth_service.js:116" in detail


async def _async_result(value):
    return value
