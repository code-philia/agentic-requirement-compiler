import re
from typing import Any

from agents.tools.cli_tools import parse_test_results

TEST_TYPE_ORDER = ["Unit", "Integration", "E2E"]
DEFAULT_TDD_TEST_BUDGET = 20


def has_visual_reference_hint(requirement_data: dict[str, Any]) -> bool:
    visual_reference = requirement_data.get("visual_reference") or []
    if visual_reference:
        return True
    description = str(requirement_data.get("description", "") or "")
    return bool(re.search(r"!\[[^\]]*\]\(([^)]+)\)", description))


def classify_non_leaf_work(requirement_data: dict[str, Any]) -> str:
    scenarios = requirement_data.get("scenarios") or []
    if scenarios:
        return "non_leaf_full"
    if has_visual_reference_hint(requirement_data):
        return "non_leaf_ui_only"
    return "skip"


def build_base_node_session(
    node_id: str,
    requirement_data: dict[str, Any],
    design_mode: str,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "design_mode": design_mode,
        "phase_status": {
            "design": "pending",
            "test": "pending",
            "implement": "pending",
        },
        "requirement_snapshot": {
            "name": requirement_data.get("name", ""),
            "description": requirement_data.get("description", ""),
            "children_ids": requirement_data.get("children_ids") or [],
            "dependencies": requirement_data.get("dependencies") or [],
        },
        "recent_failure_summary": "",
        "subtree_invariants": [],
        "assembly_boundaries": [],
    }


def build_test_plan(tests: list[dict[str, Any]]) -> dict[str, Any]:
    grouped = {
        "unit_files": [],
        "integration_files": [],
        "e2e_files": [],
        "test_ids": [],
    }
    for test in tests:
        if not isinstance(test, dict):
            continue
        test_type = str(test.get("type", "")).strip()
        file_path = str(test.get("file_path", "")).strip()
        test_id = str(test.get("test_id", "")).strip()
        if test_id:
            grouped["test_ids"].append(test_id)
        if test_type == "Unit" and file_path and file_path not in grouped["unit_files"]:
            grouped["unit_files"].append(file_path)
        elif test_type == "Integration" and file_path and file_path not in grouped["integration_files"]:
            grouped["integration_files"].append(file_path)
        elif test_type == "E2E" and file_path and file_path not in grouped["e2e_files"]:
            grouped["e2e_files"].append(file_path)
    return grouped


def summarize_batch_output(batch_output: str, max_lines: int = 30) -> str:
    lines = [line for line in (batch_output or "").splitlines() if line.strip()]
    if not lines:
        return ""
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        lines.insert(0, "...[truncated]")
    return "\n".join(lines)


def get_selected_test_types() -> list[str]:
    return list(TEST_TYPE_ORDER)


def canonicalize_test_id(
    node_id: str,
    test_type: str,
    raw_test_id: str,
    sequence: int,
) -> str:
    sanitized_node_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(node_id or "").strip()) or "NODE"
    sanitized_type = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(test_type or "").strip()) or "TEST"
    sanitized_raw = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(raw_test_id or "").strip()) or "TEST"
    return f"{sanitized_node_id}::{sanitized_type}::{sequence:03d}::{sanitized_raw}"


def build_group_handoff_summary(
    node_id: str,
    test_type: str,
    modified_files: list[str],
    group_statuses: dict[str, bool | None],
) -> str:
    passed_tests = sorted(test_id for test_id, status in group_statuses.items() if status is True)
    failed_tests = sorted(test_id for test_id, status in group_statuses.items() if status is not True)
    summary_lines = [
        f"- Previous group: {test_type}",
        f"- Modified files: {', '.join(sorted(modified_files)) if modified_files else 'none'}",
        f"- Tests passed in previous group: {', '.join(passed_tests[:12]) if passed_tests else 'none'}",
        f"- Remaining failing tests from previous group: {', '.join(failed_tests[:12]) if failed_tests else 'none'}",
    ]
    return "\n".join(summary_lines)


def map_statuses_from_batch_output(
    tests: list[dict[str, Any]],
    batch_output: str,
) -> tuple[bool, dict[str, bool | None]]:
    grouped_tests: dict[str, list[dict[str, Any]]] = {}
    for test in tests:
        file_path = str(test.get("file_path", "")).strip()
        if file_path:
            grouped_tests.setdefault(file_path, []).append(test)

    parsed_result = parse_test_results(batch_output)
    batch_passed = parsed_result.get("exit_code") == 0
    file_batch_statuses = extract_file_batch_statuses(parsed_result)
    status_by_test_id: dict[str, bool | None] = {}
    all_passed = True

    for file_path, file_tests in grouped_tests.items():
        normalized_file_path = file_path.replace("\\", "/")
        file_passed = file_batch_statuses.get(normalized_file_path, batch_passed)
        file_statuses = _map_file_test_statuses(file_tests, file_passed)
        status_by_test_id.update(file_statuses)
        if not all(value is True for value in file_statuses.values()):
            all_passed = False

    return all_passed, status_by_test_id


def prepend_agent_batch_summary(test_files: list[str], raw_output: str) -> str:
    parsed = parse_test_results(raw_output)
    lines: list[str] = []

    grouped_sub_batches = group_sub_batches_by_requested_file(parsed)
    for file_path in test_files:
        normalized_file_path = file_path.replace("\\", "/")
        sub_batch = grouped_sub_batches.get(normalized_file_path)
        lines.append(f"Test File: {file_path}")
        lines.append("Test Results:")
        if not sub_batch:
            lines.append(raw_output.rstrip())
            lines.append("")
            continue
        lines.append(str(sub_batch.get("raw_output", "")).rstrip())
        lines.append("")

    if not grouped_sub_batches:
        return raw_output

    return "\n".join(lines).rstrip()


def extract_file_batch_statuses(parsed_result: dict[str, Any]) -> dict[str, bool]:
    status_by_file: dict[str, bool] = {}
    for sub_batch in parsed_result.get("sub_batches", []) or []:
        if not isinstance(sub_batch, dict):
            continue
        sub_batch_passed = int(sub_batch.get("exit_code", 1)) == 0
        for file_path in sub_batch.get("requested_files", []) or []:
            normalized = str(file_path or "").strip().replace("\\", "/")
            if normalized:
                status_by_file[normalized] = sub_batch_passed
    return status_by_file


def group_sub_batches_by_requested_file(parsed_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for sub_batch in parsed_result.get("sub_batches", []) or []:
        if not isinstance(sub_batch, dict):
            continue
        for file_path in sub_batch.get("requested_files", []) or []:
            normalized = str(file_path or "").strip().replace("\\", "/")
            if normalized:
                grouped[normalized] = sub_batch
        if not sub_batch.get("requested_files"):
            raw_output = str(sub_batch.get("raw_output", "")).strip()
            match = re.search(r"Requested Test File:\s*(.+)", raw_output)
            if match:
                normalized = match.group(1).strip().replace("\\", "/")
                if normalized:
                    grouped[normalized] = sub_batch
    return grouped


def collect_test_files(tests: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for test in tests:
        file_path = str(test.get("file_path", "")).strip()
        if file_path and file_path not in seen:
            seen.append(file_path)
    return seen


def merge_req_ids(existing: dict[str, Any] | None, node_id: str) -> list[str]:
    req_ids = list(existing.get("req_ids", [])) if existing else []
    if node_id not in req_ids:
        req_ids.append(node_id)
    return req_ids


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _map_file_test_statuses(
    file_tests: list[dict[str, Any]],
    file_passed: bool,
) -> dict[str, bool | None]:
    return {
        str(test.get("test_id", "")).strip(): file_passed
        for test in file_tests
        if str(test.get("test_id", "")).strip()
    }
