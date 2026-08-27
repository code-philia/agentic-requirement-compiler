from __future__ import annotations

import re
from typing import Any


_INVALID_TEST_PATTERNS = (
    ("no_tests_collected", r"\(0 test\)|Tests\s+no tests|No tests found"),
    ("invalid_test_module", r"Vitest cannot be imported in a CommonJS module|failed to load (?:test|suite)"),
    ("invalid_fixture", r"unknown fixture|has unknown parameter|fixture .{0,80} not found"),
    ("ambiguous_oracle", r"strict mode violation|resolved to \d+ elements"),
)
_HARNESS_PATTERNS = (
    ("e2e_preparation", r"E2E database preparation failed"),
    ("runtime_startup", r"Backend did not become ready|backend startup.{0,80}timed out|EADDRINUSE"),
)


def parse_test_results(test_output: str) -> dict[str, Any]:
    """Parse ARC test-run output into a compact status structure."""

    result: dict[str, Any] = {"passed": [], "failed": [], "exit_code": -1, "sub_batches": []}
    output = test_output or ""
    for line in output.splitlines():
        if "Exit Code:" not in line:
            continue
        try:
            result["exit_code"] = int(line.split("Exit Code:", 1)[1].strip())
        except ValueError:
            result["exit_code"] = -1
        break

    test_file_sections = re.findall(
        r"Test File:\s*(.+?)\r?\nTest Results:\r?\n(.*?)(?=\r?\nTest File: |\Z)",
        output,
        re.DOTALL,
    )
    for file_path, raw_section in test_file_sections:
        result["sub_batches"].append(
            {
                "requested_files": [file_path.strip().replace("\\", "/")],
                "exit_code": _extract_exit_code(raw_section),
                "raw_output": raw_section.strip(),
            }
        )

    if not result["sub_batches"]:
        requested_files = [
            line.split("-", 1)[1].strip().replace("\\", "/")
            for line in output.splitlines()
            if line.startswith("- ")
        ]
        for label in ("Backend Vitest Batch", "Frontend Vitest Batch", "Playwright E2E Batch"):
            section = _extract_labeled_section(output, label)
            if not section:
                continue
            result["sub_batches"].append(
                {
                    "requested_files": requested_files,
                    "exit_code": _extract_exit_code(section),
                    "raw_output": section.strip(),
                }
            )

    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("PASS ", "✓", "√", "✔")):
            result["passed"].append(stripped)
        elif stripped.startswith(("FAIL ", "✗", "×", "✕")) or " FAILED" in stripped:
            result["failed"].append(stripped)
    return result


def classify_red_result(test_output: str) -> dict[str, str]:
    """Conservatively decide whether a pre-implementation failure is a usable RED."""
    parsed = parse_test_results(test_output)
    if parsed["exit_code"] == 0:
        return {"kind": "baseline_green", "reason": "The test already passes on the pre-requirement baseline."}
    output = test_output or ""
    for reason, pattern in _INVALID_TEST_PATTERNS:
        if re.search(pattern, output, re.IGNORECASE | re.DOTALL):
            return {"kind": "invalid_test", "reason": reason}
    for reason, pattern in _HARNESS_PATTERNS:
        if re.search(pattern, output, re.IGNORECASE | re.DOTALL):
            return {"kind": "invalid_harness", "reason": reason}
    return {
        "kind": "valid_red",
        "reason": "The runner and harness reached a behavior-level failure.",
    }


def _extract_exit_code(output: str) -> int:
    for line in (output or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("Exit Code:"):
            continue
        try:
            return int(stripped.split("Exit Code:", 1)[1].strip())
        except ValueError:
            return -1
    return -1


def _extract_labeled_section(output: str, label: str) -> str:
    pattern = rf"=== {re.escape(label)} ===\r?\n(.*?)(?=\r?\n=== |\Z)"
    match = re.search(pattern, output or "", re.DOTALL)
    return match.group(1).strip() if match else ""
