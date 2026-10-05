from __future__ import annotations

import re
from typing import Any


def repair_execution_feedback(output: str) -> str:
    """Model-facing evidence, separate from complete operational/debug output."""
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output or "")
    sections = re.split(r"(?=^=== )", clean, flags=re.MULTILINE)
    evidence = []
    for section in sections:
        if section.startswith(("=== E2E Browser Evidence", "=== E2E Failure Page Snapshot")):
            evidence.append(section.strip())
    if not evidence:
        # Preserve startup/build/SQL failures when no browser evidence exists.
        return compact_execution_output(clean, max_chars=8000, max_lines=100)
    lines = clean.splitlines()
    selected = set()
    for i, line in enumerate(lines):
        if re.search(r"Test timeout|Error: locator|^\s*\d+\).*›|^\s*FAIL\b|^\s*Expected:|^\s*Received:", line):
            selected.update(range(max(0, i - 1), min(len(lines), i + 18)))
    runner = "\n".join(lines[i] for i in sorted(selected))
    codes = re.findall(r"^\s*Exit Code:\s*(-?\d+)\s*$", clean, re.MULTILINE)
    status = next((code for code in codes if code != "0"), codes[0] if codes else "missing")
    return (f"Exit Code: {status}\n" + runner[:3500] + "\n\n" + "\n\n".join(evidence)[:8500]).strip()


def compact_execution_output(output: str, *, max_chars: int = 24000, max_lines: int = 240) -> str:
    """Keep status and error/stack neighborhoods instead of slicing the log tail."""
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output or "")
    lines = [line for line in clean.splitlines() if line.strip()]
    if len(clean) <= max_chars and len(lines) <= max_lines:
        return "\n".join(lines)
    status = [i for i, line in enumerate(lines) if re.search(
        r"Exit Code:|^=== |^Runner:|^Batch Test Type:|^Requested Test Files:|^Source paths:", line)]
    errors = [i for i, line in enumerate(lines) if re.search(
        r"\b(?:FAIL|FAILED|Error|TypeError|AssertionError|SQLITE_\w+)\b|failed|timed out|Timeout|^\s*(?:at |❯)|Expected:|Received:", line)]
    # Cover every error first, then expand neighborhoods evenly across failures.
    priorities = list(dict.fromkeys(status + errors))
    for distance in range(1, 7):
        priorities.extend(index for i in errors for index in (i - distance, i + distance)
                          if 0 <= index < len(lines))
    priorities.extend(range(min(12, len(lines))))
    priorities.extend(range(max(0, len(lines) - 8), len(lines)))
    selected: set[int] = set()
    size = 0
    for i in dict.fromkeys(priorities):
        cost = len(lines[i]) + 48  # Allow room for omission markers between selected lines.
        if len(selected) < max_lines and size + cost <= max_chars - 1000:
            selected.add(i)
            size += cost
    result = []
    previous = -1
    for i in sorted(selected):
        if i > previous + 1:
            result.append(f"...[omitted {i - previous - 1} lines]...")
        result.append(lines[i])
        previous = i
    if previous < len(lines) - 1:
        result.append(f"...[omitted {len(lines) - previous - 1} lines]...")
    return "\n".join(result)


def parse_test_results(test_output: str) -> dict[str, Any]:
    """Parse ARC test-run output into a compact status structure."""

    result: dict[str, Any] = {"passed": [], "failed": [], "exit_code": -1, "sub_batches": []}
    output = test_output or ""
    codes = [int(code) for code in re.findall(r"^\s*Exit Code:\s*(-?\d+)\s*$", output, re.MULTILINE)]
    if codes:
        # A successful build section cannot conceal failed preparation/test sections.
        result["exit_code"] = next((code for code in codes if code != 0), 0)

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
