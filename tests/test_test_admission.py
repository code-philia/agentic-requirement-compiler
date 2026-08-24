from pathlib import Path
from types import SimpleNamespace

import pytest

from app_type_handler.web import WebAppType
from core.phases import WorkflowPhaseRunner


def _runner(tmp_path: Path) -> WorkflowPhaseRunner:
    runner = object.__new__(WorkflowPhaseRunner)
    runner.workspace_path = str(tmp_path)
    runner.app_handler = WebAppType(str(tmp_path), "", None, lambda *_: None)
    return runner


def test_prepare_tests_requires_each_requirement_scenario_in_e2e_manifest(tmp_path: Path):
    test_path = tmp_path / "backend" / "test-e2e" / "login.e2e.test.ts"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "import { expect, test } from '@playwright/test';\n"
        "test('valid login', async ({ page }) => {\n"
        "  await page.goto('/login');\n"
        "  await expect(page.getByRole('status')).toContainText('Signed in');\n"
        "});\n",
        encoding="utf-8",
    )
    tests = [
        {
            "test_id": "REQ-1-E2E-1",
            "type": "E2E",
            "file_path": "backend/test-e2e/login.e2e.test.ts",
            "scenario_id": "Valid login",
        }
    ]

    with pytest.raises(ValueError, match="Invalid login"):
        _runner(tmp_path)._prepare_tests(
            node_id="REQ-1",
            tests=tests,
            requirement_data={"scenarios": [{"name": "Valid login"}, {"name": "Invalid login"}]},
        )


def test_prepare_tests_accepts_complete_scenario_traceability(tmp_path: Path):
    test_path = tmp_path / "backend" / "test-e2e" / "login.e2e.test.ts"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "import { expect, test } from '@playwright/test';\n"
        "test('login paths', async ({ page }) => {\n"
        "  await page.goto('/login');\n"
        "  await expect(page.getByRole('status')).toContainText('Guest');\n"
        "});\n",
        encoding="utf-8",
    )
    tests = [
        {
            "test_id": "REQ-1-E2E-1",
            "type": "E2E",
            "file_path": "backend/test-e2e/login.e2e.test.ts",
            "scenario_id": "Valid login",
        },
        {
            "test_id": "REQ-1-E2E-2",
            "type": "E2E",
            "file_path": "backend/test-e2e/login.e2e.test.ts",
            "scenario_id": "Invalid login",
        },
    ]

    stored = _runner(tmp_path)._prepare_tests(
        node_id="REQ-1",
        tests=tests,
        requirement_data={"scenarios": [{"name": "Valid login"}, {"name": "Invalid login"}]},
    )

    assert {item["scenario_id"] for item in stored} == {"Valid login", "Invalid login"}


def test_store_prepared_tests_persists_scenario_traceability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    stored: list[dict] = []

    class Traceability:
        def upsert_test(self, **fields):
            stored.append(fields)

    monkeypatch.setattr(
        "core.phases.get_runtime",
        lambda: SimpleNamespace(traceability=Traceability()),
    )
    runner = _runner(tmp_path)

    runner._store_prepared_tests(
        [
            {
                "test_id": "REQ-1-E2E-1",
                "req_id": "REQ-1",
                "interface_ids": ["REQ-1-UI"],
                "type": "E2E",
                "file_path": "backend/test-e2e/login.spec.ts",
                "first_line": "1",
                "scenario_id": "Valid login",
            }
        ]
    )

    assert stored[0]["scenario_id"] == "Valid login"


def test_design_retry_preserves_omitted_prior_test_layers(tmp_path: Path):
    runner = _runner(tmp_path)
    paths = (
        tmp_path / "backend/tests/auth/unit.test.js",
        tmp_path / "backend/tests/auth/routes.integration.test.js",
        tmp_path / "backend/test-e2e/auth.e2e.spec.ts",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test('covered', () => {})\n", encoding="utf-8")

    generated = [
        {
            "test_id": "REQ-E2E",
            "req_id": "REQ-1.1",
            "type": "E2E",
            "file_path": "backend/test-e2e/auth.e2e.spec.ts",
        }
    ]
    prior = [
        {
            "test_id": "REQ-UNIT",
            "req_id": "REQ-1.1",
            "type": "Unit",
            "file_path": "backend/tests/auth/unit.test.js",
            "passed": True,
        },
        {
            "test_id": "REQ-INTEGRATION",
            "req_id": "REQ-1.1",
            "type": "Integration",
            "file_path": "backend/tests/auth/routes.integration.test.js",
            "passed": True,
        },
    ]

    merged, preserved_layers = runner._preserve_missing_prior_test_layers(generated, prior)

    assert preserved_layers == ["Unit", "Integration"]
    assert [item["type"] for item in merged] == ["E2E", "Unit", "Integration"]
    assert all(item.get("passed") is None for item in merged[1:])


def test_design_retry_does_not_preserve_missing_test_files(tmp_path: Path):
    runner = _runner(tmp_path)

    merged, preserved_layers = runner._preserve_missing_prior_test_layers(
        [],
        [
            {
                "test_id": "STALE-UNIT",
                "req_id": "REQ-1.1",
                "type": "Unit",
                "file_path": "backend/tests/auth/deleted.test.js",
            }
        ],
    )

    assert merged == []
    assert preserved_layers == []


def test_second_generation_attempt_keeps_valid_layer_from_rejected_attempt(tmp_path: Path):
    runner = _runner(tmp_path)
    integration_path = tmp_path / "backend/tests/auth/service.integration.test.js"
    e2e_path = tmp_path / "backend/test-e2e/auth.e2e.spec.ts"
    for path in (integration_path, e2e_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test('covered', () => expect(1).toBe(1));\n", encoding="utf-8")

    rejected_attempt = [
        {
            "test_id": "REQ-INTEGRATION",
            "req_id": "REQ-1.1",
            "type": "Integration",
            "file_path": "backend/tests/auth/service.integration.test.js",
        },
        {
            "test_id": "REQ-E2E-OLD",
            "req_id": "REQ-1.1",
            "type": "E2E",
            "file_path": "backend/test-e2e/auth.e2e.spec.ts",
        },
    ]
    corrected_attempt = [
        {
            "test_id": "REQ-E2E-NEW",
            "req_id": "REQ-1.1",
            "type": "E2E",
            "file_path": "backend/test-e2e/auth.e2e.spec.ts",
        }
    ]

    merged, preserved_layers = runner._preserve_missing_prior_test_layers(
        corrected_attempt,
        rejected_attempt,
    )

    assert preserved_layers == ["Integration"]
    assert [item["test_id"] for item in merged] == ["REQ-E2E-NEW", "REQ-INTEGRATION"]


def test_prepare_tests_requires_owned_interface_layers(tmp_path: Path):
    e2e_path = tmp_path / "backend/test-e2e/auth.e2e.spec.ts"
    e2e_path.parent.mkdir(parents=True)
    e2e_path.write_text(
        "import { expect, test } from '@playwright/test';\n"
        "test('register', async ({ page }) => {\n"
        "  await page.goto('/register');\n"
        "  await expect(page.getByRole('main')).toBeVisible();\n"
        "});\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unit, Integration"):
        _runner(tmp_path)._prepare_tests(
            node_id="REQ-1.1",
            tests=[
                {
                    "test_id": "REQ-E2E",
                    "type": "E2E",
                    "file_path": "backend/test-e2e/auth.e2e.spec.ts",
                }
            ],
            required_test_types=["Unit", "Integration", "E2E"],
        )
