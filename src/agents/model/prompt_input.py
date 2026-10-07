"""Readable layered model inputs; output contracts remain machine-readable JSON."""
from __future__ import annotations

import json
import re
from typing import Any

from .tool_sequence import tool_return_examples

def format_task_input(task: dict[str, Any], schema: dict[str, Any]) -> str:
    remaining = dict(task)
    # Render one path inventory, while leaving the task's authoritative scopes and
    # snapshots intact for deterministic write validation and bounded read loops.
    inventory: dict[str, dict[str, Any]] = {}

    def file_record(path: str) -> dict[str, Any]:
        return inventory.setdefault(path, {})

    context = remaining.get("context", "")
    if isinstance(context, str):
        def locations(match: re.Match[str]) -> str:
            for entry in json.loads(match[1]):
                path = entry.get("file_path")
                if path:
                    file_record(path).update({key: value for key, value in entry.items() if key != "file_path"})
            return ""
        remaining["context"] = re.sub(r"<file_locations>\n(.*?)\n</file_locations>", locations, context, flags=re.S).strip()
    scope = remaining.get("implementation_scope")
    if isinstance(scope, dict):
        for layer, paths in scope.get("backend", {}).items():
            for path in paths:
                file_record(path)["layer"] = layer
        for key in ("frontend_files", "shared", "test_asset_files", "dependency_manifests", "allowed_files"):
            for path in scope.get(key, []):
                file_record(path)["writable"] = True
                if key != "allowed_files":
                    file_record(path)[key] = True
        remaining["implementation_scope"] = {key: value for key, value in scope.items()
            if key not in {"backend", "frontend_files", "shared", "test_asset_files", "dependency_manifests", "allowed_files"}}
    for key, flag in (("file_index", "indexed"), ("required_source_files", "required"),
                      ("protected_files", "protected"), ("excluded_sources", "excluded"),
                      ("requested_sources", "requested"), ("missing_requested_sources", "missing"),
                      ("missing_tracked_files", "missing")):
        for path in remaining.pop(key, []):
            file_record(path)[flag] = True
    supplied = remaining.get("sources", {})
    for record in inventory.values():
        record.pop("indexed", None)
        if record.get("protected"):
            record.pop("writable", None)
    # Source headings already establish availability; retain only useful metadata.
    inventory = {path: record for path, record in inventory.items() if record or path not in supplied}
    if inventory:
        remaining["file_inventory"] = inventory
    sections = [
        "# Task brief\nRead priority: current task and failure feedback, then write boundaries, "
        "then requirement and evidence."]

    def section(title: str, keys: tuple[str, ...]) -> None:
        values = [(key, remaining.pop(key)) for key in keys if key in remaining]
        if not values:
            return
        parts = ["## " + title]
        for key, value in values:
            if value is None or value == "" or value == [] or value == {}:
                continue
            parts.append("### " + key)
            parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
        if len(parts) > 1:
            sections.append("\n".join(parts))

    section("1. Current task", ("model_stage", "phase", "node_id", "app_type", "test_type", "test_intent", "replace_test_id"))
    section("2. Priority feedback — fix the cause and preserve valid behavior", ("feedback", "previous_failure", "shared_read_status"))
    section("3. Write boundaries and agent budget", ("implementation_scope", "agent_budget"))
    section("4. Authoritative requirement", ("requirement", "requirements", "requirement_tree", "shared_need"))
    section("5. Acceptance, dependencies and runtime contracts", ("context", "prepared_database", "capability", "selected_shared_contracts"))
    section("6. Discovery index and file availability", ("directory_structure", "shared_index", "shared_index_pages", "file_inventory", "index_truncated"))
    section("7. Registered tests", ("tests", "existing_tests"))
    sources = dict(remaining.pop("sources", {}))
    observations = remaining.pop("file_observations", [])
    results = remaining.pop("last_read_results", [])
    if observations or results:
        parts = ["## 8. read_file observations"]
        for observation in observations:
            path = observation["path"]
            if path not in sources:
                continue
            content = sources.pop(path)
            fence = "```"
            while fence in content:
                fence += "`"
            parts.extend(["### read_file result: " + path,
                          "status: ok\n" + fence + "text\n" + content + "\n" + fence])
        for result in results:
            if result.get("status") != "ok":
                parts.append(json.dumps(result, ensure_ascii=False))
        sections.append("\n".join(parts))
    if sources:
        parts = ["## 8. Source snapshots"]
        for path, content in sources.items():
            # A variable fence preserves literal source even when it contains Markdown fences.
            fence = "```"
            while fence in content:
                fence += "`"
            parts.extend(["### File: " + path, fence + "text\n" + content + "\n" + fence])
        sections.append("\n".join(parts))
    section("9. Rejected candidate — repair only; it was not applied", ("previous_candidate",))
    if remaining:
        section("10. Additional task records", tuple(remaining))
    sections.append("## 11. Tool definitions — reference only, do not return this section\nReturn only a JSON array of calls. Each call contains tool and its parameters directly. "
                    "The contract's parameters field describes allowed arguments; it is NOT an output field. "
                    "Never wrap call arguments under parameters, arguments, input or function. "
                    "Omit unused optional parameters. No wrapper object, explanation, status or summary. "
                    "Use [] when no action is needed.\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
    sections.append("## 12. Placeholder return examples — choose only the needed calls\n"
                    "Each named example below is a SEPARATE response array, not a wrapper to return. "
                    "Return the array itself. Replace every <actual ...> placeholder with real values "
                    "from this task/source; do not write placeholders into files. Enum/boolean/number "
                    "examples show valid JSON types, not required business values. These are minimum "
                    "field shapes, not complete implementations; include required business keys and "
                    "permitted optional fields where needed. Do not copy example tools indiscriminately. "
                    "Reads must be returned without writes/registrations; request_shared and "
                    "report_database_gap must each be returned alone. A final write batch may combine "
                    "writes with applicable registration/declaration calls. DESIGN needs exactly one "
                    "declare_identity_usage; TestGenerator must register its tests. Escape code strings "
                    "as JSON: use \\n for a newline, \\\" for a quote and \\\\ for a backslash.\n" +
                    "\n".join(name + ":\n" + json.dumps(example, ensure_ascii=False)
                              for name, example in tool_return_examples(schema).items()))
    return "\n\n".join(sections)
