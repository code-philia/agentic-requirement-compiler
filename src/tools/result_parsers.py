from __future__ import annotations

from typing import Any
import re


def normalize_test_manifest_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize structured or fallback agent output into test manifest items."""

    candidates = payload.get("tests")
    if candidates is None:
        candidates = payload.get("items")
    if candidates is None and _looks_like_test_item(payload):
        candidates = [payload]
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if isinstance(item, dict) and _looks_like_test_item(item)]


def _looks_like_test_item(value: dict[str, Any]) -> bool:
    return bool(
        str(value.get("test_id", "")).strip()
        or str(value.get("file_path", "")).strip()
        or str(value.get("type", "")).strip()
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
