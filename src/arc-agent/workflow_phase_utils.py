import json
import re
from typing import Any

from agents.tools.cli_tools import parse_test_results
from traceability.database import (
    get_interfaces_by_req_id,
    get_node_contract,
    get_node_state,
    get_tests_by_req_id,
)

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
    node_role: str,
    design_mode: str,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "node_role": node_role,
        "design_mode": design_mode,
        "phase_status": {
            "understand": "pending",
            "design": "pending",
            "spec": "pending",
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


def determine_non_leaf_result_state(requirement_data: dict[str, Any]) -> str:
    child_ids = [
        str(child_id).strip()
        for child_id in (requirement_data.get("children_ids") or [])
        if str(child_id).strip()
    ]
    for child_id in child_ids:
        child_state = get_node_state(child_id) or {}
        if str(child_state.get("state", "")).strip().upper() == "FAILED":
            return "CONVERGED_WITH_FAILED_CHILDREN"
    return "CONVERGED"


def get_non_leaf_gate_failures(requirement_data: dict[str, Any]) -> list[str]:
    blocking: list[str] = []
    child_ids = [
        str(child_id).strip()
        for child_id in (requirement_data.get("children_ids") or [])
        if str(child_id).strip()
    ]
    for child_id in child_ids:
        child_state = get_node_state(child_id) or {}
        normalized_state = str(child_state.get("state", "")).strip().upper()
        if normalized_state not in {"PASSED", "CONVERGED", "CONVERGED_WITH_FAILED_CHILDREN"}:
            blocking.append(f"{child_id}:{normalized_state or 'UNSEEN'}")
        elif normalized_state == "CONVERGED_WITH_FAILED_CHILDREN":
            blocking.append(f"{child_id}:{normalized_state}")
    return blocking


def build_non_leaf_scope_note() -> str:
    return (
        "This is a parent integration and validation batch for a non-leaf node. "
        "You may only edit parent-owned assembly files such as routes, layouts, providers, page containers, "
        "mount points, guards, and shared shell composition boundaries. Do not re-implement child business logic, "
        "do not invent fake session/data fallbacks, and do not mask a child failure from the parent shell."
    )


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
    contract_row = get_node_contract(node_id)
    contract = contract_row.get("content", {}) if isinstance(contract_row, dict) else {}
    canonical_routes = contract.get("canonical_routes") or []
    if canonical_routes:
        summary_lines.append(f"- Canonical routes for this node: {', '.join(canonical_routes[:10])}")
    auth_expectation = contract.get("auth_expectation", "")
    if auth_expectation:
        summary_lines.append(f"- Auth expectation: {auth_expectation}")
    return "\n".join(summary_lines)


def build_frozen_node_contract(
    node_id: str,
    requirement_data: dict[str, Any],
    interfaces: list[dict[str, Any]],
    tests: list[dict[str, Any]],
) -> dict[str, Any]:
    is_leaf = not bool(requirement_data.get("children_ids"))
    interface_summaries = []
    canonical_routes: list[str] = []
    shared_shell_targets: list[str] = []
    provider_hints: list[str] = []
    navigation_targets: list[str] = []
    mount_points: list[str] = []
    data_boundaries: list[str] = []
    auth_expectation = "unspecified"

    for interface in interfaces:
        if not isinstance(interface, dict):
            continue
        file_path = str(interface.get("file_path", "")).strip()
        first_line = str(interface.get("first_line", "")).strip()
        description = str(interface.get("description", "") or "")
        iface_type = str(interface.get("type", "")).strip()
        interface_summaries.append(
            {
                "interface_id": str(interface.get("interface_id", "")).strip(),
                "type": iface_type,
                "file_path": file_path,
                "first_line": first_line,
            }
        )

        for candidate in re.findall(r'["\'](/[^"\']+)["\']', f"{first_line}\n{description}\n{file_path}"):
            if candidate not in canonical_routes:
                canonical_routes.append(candidate)

        normalized_path = file_path.replace("\\", "/")
        if any(token in normalized_path for token in ("app.", "/app.", "main.", "/main.", "routes/", "router", "layout", "provider")):
            if file_path and file_path not in shared_shell_targets:
                shared_shell_targets.append(file_path)

        if "provider" in normalized_path.lower() or "provider" in first_line.lower():
            if file_path and file_path not in provider_hints:
                provider_hints.append(file_path)

        lowered_blob = f"{iface_type}\n{first_line}\n{description}\n{file_path}".lower()
        if any(token in lowered_blob for token in ("route", "navigate", "link", "href", "path")):
            for candidate in re.findall(r'["\'](/[^"\']+)["\']', f"{first_line}\n{description}"):
                if candidate not in navigation_targets:
                    navigation_targets.append(candidate)
        if any(token in lowered_blob for token in ("slot", "mount", "outlet", "layout", "container")):
            marker = file_path or first_line or str(interface.get("name", "")).strip()
            if marker and marker not in mount_points:
                mount_points.append(marker)
        if any(token in lowered_blob for token in ("context", "provider", "session", "auth", "boundary", "data", "api")):
            marker = str(interface.get("interface_id", "")).strip() or file_path or first_line
            if marker and marker not in data_boundaries:
                data_boundaries.append(marker)

    for test in tests:
        if not isinstance(test, dict):
            continue
        file_path = str(test.get("file_path", "")).strip()
        first_line = str(test.get("first_line", "")).strip()
        for candidate in re.findall(r'["\'](/[^"\']+)["\']', f"{file_path}\n{first_line}"):
            if candidate not in canonical_routes:
                canonical_routes.append(candidate)

    requirement_blob = json.dumps(requirement_data, ensure_ascii=False).lower()
    if "login" in requirement_blob or "authenticated" in requirement_blob or "logout" in requirement_blob:
        auth_expectation = "auth-sensitive"
    if "without login" in requirement_blob or "unauthenticated" in requirement_blob:
        auth_expectation = "explicit-unauthenticated-flow"

    if not shared_shell_targets and not is_leaf:
        for fallback in ("frontend/src/App.tsx", "frontend/src/main.tsx", "backend/src/app.js"):
            shared_shell_targets.append(fallback)

    return {
        "req_id": node_id,
        "node_role": "leaf" if is_leaf else "non_leaf",
        "children_ids": requirement_data.get("children_ids") or [],
        "interface_count": len(interface_summaries),
        "interfaces": interface_summaries,
        "test_files": sorted(
            {
                str(test.get("file_path", "")).strip()
                for test in tests
                if isinstance(test, dict) and str(test.get("file_path", "")).strip()
            }
        ),
        "canonical_routes": canonical_routes[:20],
        "auth_expectation": auth_expectation,
        "provider_hints": provider_hints[:10],
        "shared_shell_targets": shared_shell_targets[:12],
        "navigation_targets": navigation_targets[:20],
        "mount_points": mount_points[:20],
        "data_boundaries": data_boundaries[:20],
        "assembly_scope": (
            [
                "app shell",
                "router / route container",
                "top-level layout / page container",
                "shared provider composition",
                "child mounting points",
            ]
            if not is_leaf
            else ["leaf feature implementation"]
        ),
    }


def build_non_leaf_convergence_summary(node_id: str, requirement_data: dict[str, Any]) -> str:
    child_ids = [str(child_id).strip() for child_id in (requirement_data.get("children_ids") or []) if str(child_id).strip()]
    if not child_ids:
        return "- No child nodes were found."

    lines = [
        "- This parent node should converge child capabilities into one coherent subsystem.",
        "- Use concrete child outputs as assembly inputs: implemented interfaces, landed files, passed tests, and remaining failures.",
    ]
    contract_row = get_node_contract(node_id)
    contract = contract_row.get("content", {}) if isinstance(contract_row, dict) else {}
    assembly_scope = contract.get("assembly_scope") or []
    shared_shell_targets = contract.get("shared_shell_targets") or []
    provider_hints = contract.get("provider_hints") or []
    canonical_routes = contract.get("canonical_routes") or []
    navigation_targets = contract.get("navigation_targets") or []
    mount_points = contract.get("mount_points") or []
    data_boundaries = contract.get("data_boundaries") or []
    auth_expectation = contract.get("auth_expectation", "")
    if assembly_scope:
        lines.append(f"- Parent assembly scope: {', '.join(assembly_scope)}")
    if shared_shell_targets:
        lines.append(f"- Parent shared shell targets: {', '.join(shared_shell_targets[:10])}")
    if provider_hints:
        lines.append(f"- Parent provider hints: {', '.join(provider_hints[:10])}")
    if canonical_routes:
        lines.append(f"- Parent canonical routes: {', '.join(canonical_routes[:12])}")
    if navigation_targets:
        lines.append(f"- Parent navigation targets: {', '.join(navigation_targets[:12])}")
    if mount_points:
        lines.append(f"- Parent mount points / shell slots: {', '.join(mount_points[:12])}")
    if data_boundaries:
        lines.append(f"- Parent data / context boundaries: {', '.join(data_boundaries[:12])}")
    if auth_expectation:
        lines.append(f"- Parent auth expectation: {auth_expectation}")
    lines.append("- Parent convergence must not duplicate providers, invent fake user/session fallbacks, or override child feature semantics.")
    lines.append("- Parent done gate: the parent only passes after relevant children are landed and parent integration tests plus browser-visible validation pass.")
    for child_id in child_ids:
        child_interfaces = get_interfaces_by_req_id(child_id)
        child_tests = get_tests_by_req_id(child_id)
        implemented = sorted(
            str(interface.get("interface_id", "")).strip()
            for interface in child_interfaces
            if interface.get("implemented")
        )
        child_files = sorted(
            {
                str(interface.get("file_path", "")).strip()
                for interface in child_interfaces
                if str(interface.get("file_path", "")).strip()
            }
        )
        passed_tests = sorted(
            str(test.get("test_id", "")).strip()
            for test in child_tests
            if test.get("passed") is True
        )
        failed_tests = sorted(
            str(test.get("test_id", "")).strip()
            for test in child_tests
            if test.get("passed") is False
        )
        lines.append(f"- Child `{child_id}` implemented interfaces: {', '.join(implemented[:10]) if implemented else 'none'}")
        lines.append(f"- Child `{child_id}` landed files: {', '.join(child_files[:10]) if child_files else 'none'}")
        lines.append(f"- Child `{child_id}` passed tests: {', '.join(passed_tests[:10]) if passed_tests else 'none'}")
        if failed_tests:
            lines.append(f"- Child `{child_id}` remaining failed tests: {', '.join(failed_tests[:10])}")
    return "\n".join(lines)


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
