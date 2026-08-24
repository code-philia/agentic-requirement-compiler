from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from agents.context.global_memory import GlobalProjectMemory, global_memory_enabled
from core import sessions
from core.files import write_json_file
from core.workflow import ARCWorkflowManager


_USAGE_RE = re.compile(
    r"agent usage: prompt_tokens=(\d+) completion_tokens=(\d+) total_tokens=(\d+) "
    r"cached_tokens=(\d+) llm_calls=(\d+)(?: model=([^\s]+))?"
)
_TOOL_CALL_RE = re.compile(r"tool-call>\s+([^\s]+)\s+args=(\{.*\})$")
_TEST_EXECUTION_RE = re.compile(r"`run_tests`\s+\S+\s+usage\s+\d+/\d+")
_TEST_RESULT_RE = re.compile(
    r"`run_tests`\s+(Unit|Integration|E2E)\s+(passed|failed).*?attempt\s+(\d+)/(\d+)",
    re.IGNORECASE,
)
_PROVIDER_FALLBACK_RE = re.compile(r"agent stream failed; falling back", re.IGNORECASE)


def prepare_checkpoint(
    *,
    source_workspace: str,
    output_workspace: str,
    git_ref: str,
    node_id: str,
    restore_handoff: bool = True,
) -> Path:
    """Clone one generated-project checkpoint and restore its selected node session."""
    source = Path(source_workspace).expanduser().resolve()
    output = Path(output_workspace).expanduser().resolve()
    if not (source / ".git").is_dir():
        raise ValueError(f"Source workspace is not a Git checkpoint repository: {source}")
    if output.exists():
        raise ValueError(f"Micro-run output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--quiet", "--no-hardlinks", str(source), str(output)])
    _git(["-C", str(output), "checkout", "--quiet", "--detach", git_ref])

    source_session = source / ".arc" / "node_sessions" / f"{node_id}.json"
    if source_session.is_file():
        target_session = output / ".arc" / "node_sessions" / source_session.name
        target_session.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_session, target_session)
        payload = _read_json(target_session)
        if restore_handoff:
            checkpoint_time = _git(
                ["-C", str(source), "show", "-s", "--format=%cI", git_ref],
                capture=True,
            ).strip()
            latest_handoff = _latest_model_handoff(
                source / ".arc" / "debug.log",
                node_id,
                cutoff_timestamp=checkpoint_time,
            )
            if latest_handoff:
                payload["checkpoint_handoff_summary"] = latest_handoff
        else:
            payload.pop("checkpoint_handoff_summary", None)
            payload.pop("recent_failure_summary", None)
            payload.pop("tdd_handoff", None)
            phase_status = payload.get("phase_status")
            if isinstance(phase_status, dict):
                phase_status["implement"] = "pending"
        write_json_file(str(target_session), payload)
    _reuse_dependencies(source, output)
    return output


async def execute_checkpoint_micro_run(
    *,
    workspace: str,
    requirement_path: str,
    node_id: str,
    test_type: str | None,
    run_tests_budget: int,
    max_agent_sessions: int,
    app_type: str,
    web_port: int,
    log_cb: Callable[..., Any],
    protect_test_files: bool = True,
) -> dict[str, Any]:
    """Run one or all node test layers and return a compact experiment report."""
    root = Path(workspace).expanduser().resolve()
    started = time.perf_counter()
    manager = ARCWorkflowManager(
        workspace_path=str(root),
        requirement_path=requirement_path,
        app_type=app_type,
        web_port=web_port,
        log_cb=log_cb,
    )
    await manager.prepare_resume_context()
    requirement_tree = await manager.load_requirement_tree()
    if not requirement_tree:
        raise ValueError("Could not load the requirement tree for the micro-run.")
    manager.runtime.traceability.store_requirement_tree(requirement_tree)

    tests = manager.runtime.traceability.list_tests(req_id=node_id)
    layer_tests = tests if test_type is None else [
        item
        for item in tests
        if str(item.get("type", "")).strip().casefold() == test_type.strip().casefold()
    ]
    if not layer_tests:
        label = test_type or "Unit/Integration/E2E"
        raise ValueError(f"No registered {label!r} tests found for node {node_id!r}.")
    test_files = sorted(
        {
            str(item.get("file_path", "")).strip()
            for item in layer_tests
            if str(item.get("file_path", "")).strip()
        }
    )

    session = sessions.load_node_session(node_id)
    restored_failure = str(
        session.get("checkpoint_handoff_summary")
        or session.get("recent_failure_summary", "")
        or ""
    ).strip()
    memory = GlobalProjectMemory(str(root), manager.runtime.traceability)
    if restored_failure and global_memory_enabled():
        restored_test_type = str(
            test_type
            or (session.get("tdd_handoff") or {}).get("last_test_type")
            or "unknown"
        ).strip()
        restored_test_files = [
            str(item.get("file_path", "")).strip()
            for item in tests
            if str(item.get("type", "")).strip().casefold() == restored_test_type.casefold()
            and str(item.get("file_path", "")).strip()
        ] or test_files
        memory.record_handoff(
            requirement=node_id,
            test_type=restored_test_type,
            test_files=restored_test_files,
            fingerprint=restored_failure,
            protected_files=test_files,
        )
    memory_context = memory.refresh(node_id=node_id)
    handoffs = _read_json(memory.handoff_path)
    expected_next_file = str((handoffs.get(node_id) or {}).get("next_file", ""))

    passed = await manager.phase_runner.run_checkpoint_micro_run(
        node_id=node_id,
        test_type=test_type,
        run_tests_budget=max(1, run_tests_budget),
        max_agent_sessions=max(1, max_agent_sessions),
        protect_test_files=protect_test_files,
    )
    elapsed = time.perf_counter() - started
    log_metrics = parse_micro_run_log(root / ".arc" / "debug.log")
    changed_files = _changed_files(root)
    report = {
        "ok": passed,
        "node_id": node_id,
        "test_type": test_type or "All",
        "memory_enabled": global_memory_enabled(),
        "elapsed_seconds": round(elapsed, 3),
        "run_tests_budget": max(1, run_tests_budget),
        "max_agent_sessions": max(1, max_agent_sessions),
        "test_files_protected": protect_test_files,
        "memory_bytes": len(memory_context.encode("utf-8")),
        "memory_context": memory_context,
        "restored_failure": bool(restored_failure),
        "expected_next_file": expected_next_file,
        "expected_file_read": expected_next_file in log_metrics["files_read"],
        "expected_file_changed": expected_next_file in changed_files,
        "changed_files": changed_files,
        **log_metrics,
    }
    report_path = root / ".arc" / "micro-run-report.json"
    write_json_file(str(report_path), report)
    report["report_path"] = str(report_path)
    return report


def parse_micro_run_log(path: Path) -> dict[str, Any]:
    """Extract cost/behavior signals from a fresh micro-run debug log."""
    prompt = completion = total = cached = llm_calls = 0
    model = ""
    files_read: list[str] = []
    files_written: list[str] = []
    run_tests_calls = 0
    run_tests_executions = 0
    provider_fallbacks = 0
    layer_events: dict[str, list[tuple[int, bool]]] = {}
    if not path.is_file():
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "uncached_prompt_tokens": 0,
            "cache_rate": 0.0,
            "model": "",
            "estimated_cost_usd": None,
            "estimated_uncached_cost_usd": None,
            "estimated_cache_savings_usd": None,
            "llm_calls": 0,
            "run_tests_calls": 0,
            "run_tests_executions": 0,
            "provider_fallbacks": 0,
            "layer_metrics": {},
            "files_read": [],
            "files_written": [],
        }
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _PROVIDER_FALLBACK_RE.search(line):
            provider_fallbacks += 1
        if _TEST_EXECUTION_RE.search(line):
            run_tests_executions += 1
        test_result = _TEST_RESULT_RE.search(line)
        if test_result:
            layer = test_result.group(1).upper()
            if layer == "INTEGRATION":
                layer = "Integration"
            elif layer == "UNIT":
                layer = "Unit"
            else:
                layer = "E2E"
            layer_events.setdefault(layer, []).append(
                (int(test_result.group(3)), test_result.group(2).lower() == "passed")
            )
        usage = _USAGE_RE.search(line)
        if usage:
            values = [int(value) for value in usage.groups()[:5]]
            prompt += values[0]
            completion += values[1]
            total += values[2]
            cached += values[3]
            llm_calls += values[4]
            model = usage.group(6) or model
        tool = _TOOL_CALL_RE.search(line)
        if not tool:
            continue
        name, raw_args = tool.groups()
        if name == "run_tests":
            run_tests_calls += 1
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            continue
        file_path = str(args.get("file_path") or args.get("path") or "").strip()
        if file_path.startswith("/workspace/"):
            file_path = file_path[len("/workspace/") :]
        if not file_path:
            continue
        target = files_read if name in {"read_file", "grep", "glob"} else files_written
        if name in {"read_file", "grep", "glob", "write_file", "edit_file"} and file_path not in target:
            target.append(file_path)
    cost, uncached_cost, savings = _estimate_cost(
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
    )
    layer_metrics = {}
    for layer in ("Unit", "Integration", "E2E"):
        events = layer_events.get(layer, [])
        if not events:
            continue
        passing_attempts = [attempt for attempt, passed in events if passed]
        layer_metrics[layer] = {
            "executions": len(events),
            "first_pass_attempt": min(passing_attempts) if passing_attempts else None,
            "ever_passed": bool(passing_attempts),
            "final_passed": events[-1][1],
            "pass_executions": sum(1 for _, passed in events if passed),
            "failed_executions": sum(1 for _, passed in events if not passed),
        }
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": cached,
        "uncached_prompt_tokens": max(0, prompt - cached),
        "cache_rate": round(cached / prompt, 4) if prompt else 0.0,
        "model": model,
        "estimated_cost_usd": cost,
        "estimated_uncached_cost_usd": uncached_cost,
        "estimated_cache_savings_usd": savings,
        "llm_calls": llm_calls,
        "run_tests_calls": run_tests_calls,
        "run_tests_executions": run_tests_executions,
        "provider_fallbacks": provider_fallbacks,
        "layer_metrics": layer_metrics,
        "files_read": files_read,
        "files_written": files_written,
    }


def evaluate_memory_workspace(workspace: str, node_id: str) -> dict[str, Any]:
    """Score a memory design against an existing run without invoking a model."""
    root = Path(workspace).expanduser().resolve()
    requirements_path = root / ".arc" / "traceability" / "requirements.json"
    if not requirements_path.is_file():
        raise ValueError(f"Missing ARC traceability requirements: {requirements_path}")
    traceability_root = requirements_path.parent
    store = _RequirementSnapshot(
        _read_json(requirements_path),
        interfaces=_read_json(traceability_root / "interfaces.json"),
        tests=_read_json(traceability_root / "tests.json"),
    )
    memory = GlobalProjectMemory(str(root), store)
    context = memory.refresh(node_id=node_id, write_audit=False)
    payload_text = context.removeprefix("<global_project_memory>\n").removesuffix(
        "\n</global_project_memory>"
    )
    payload = json.loads(payload_text)
    session = _read_json(root / ".arc" / "node_sessions" / f"{node_id}.json")
    restored = str(session.get("checkpoint_handoff_summary", "") or "").strip()
    if restored:
        restored_fields = memory._parse_handoff_text(restored)
        for item in payload.get("work_packets", []):
            if not isinstance(item, dict) or item.get("requirement") != node_id:
                continue
            if item.get("next_file_source") == "model_handoff":
                continue
            restored_file = str(restored_fields.get("next_file", "") or "")
            current_file = str(item.get("next_file", "") or "")
            if restored_file and ("/src/" in restored_file or "/src/" not in current_file):
                item["next_file"] = restored_file
                item["next_file_source"] = "checkpoint_handoff"
            if restored_fields.get("next_action"):
                item["next_action"] = restored_fields["next_action"]
            if restored_fields.get("observation"):
                item["observation"] = restored_fields["observation"]
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        context = "<global_project_memory>\n" + body + "\n</global_project_memory>"
    packets = [item for item in payload.get("work_packets", []) if isinstance(item, dict)]
    packet = next((item for item in packets if item.get("requirement") == node_id), {})
    checks = {
        "observation": bool(packet.get("observation")),
        "failure_signature": bool(packet.get("failure_signature")),
        "repeat_count": int(packet.get("repeat_count", 0) or 0) > 0,
        "attempted_changes": bool(packet.get("attempted_changes")),
        "do_not_repeat": bool(packet.get("do_not_repeat")),
        "next_file": bool(packet.get("next_file")),
        "next_action": bool(packet.get("next_action")),
    }
    log_metrics = parse_micro_run_log(root / ".arc" / "debug.log")
    expected_file = str(packet.get("next_file", "") or "")
    legacy_summary = str(session.get("recent_failure_summary", "") or "")
    return {
        "workspace": str(root),
        "node_id": node_id,
        "memory_bytes": len(context.encode("utf-8")),
        "work_packet": packet,
        "quality_checks": checks,
        "quality_score": round(sum(checks.values()) / len(checks), 3),
        "expected_file_read": expected_file in log_metrics["files_read"] if expected_file else False,
        "expected_file_changed": expected_file in _changed_files(root) if expected_file else False,
        "legacy_duplicate_failure_bytes_avoided": 2 * len(legacy_summary.encode("utf-8")),
    }


class _RequirementSnapshot:
    def __init__(
        self,
        rows: dict[str, Any],
        *,
        interfaces: dict[str, Any] | None = None,
        tests: dict[str, Any] | None = None,
    ) -> None:
        self.rows = rows
        self.interfaces = interfaces or {}
        self.tests = tests or {}

    def list_requirements(self) -> list[dict[str, Any]]:
        return [value for value in self.rows.values() if isinstance(value, dict)]

    def get_requirement(self, req_id: str) -> dict[str, Any] | None:
        value = self.rows.get(req_id)
        return value if isinstance(value, dict) else None

    def list_interfaces(self, req_id: str | None = None) -> list[dict[str, Any]]:
        rows = [value for value in self.interfaces.values() if isinstance(value, dict)]
        if not req_id:
            return rows
        return [row for row in rows if req_id in (row.get("req_ids") or [])]

    def list_tests(self, req_id: str | None = None) -> list[dict[str, Any]]:
        rows = [value for value in self.tests.values() if isinstance(value, dict)]
        if not req_id:
            return rows
        return [
            row
            for row in rows
            if str(row.get("req_id", "") or "") == req_id
            or req_id in (row.get("req_ids") or [])
        ]


def _estimate_cost(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
) -> tuple[float | None, float | None, float | None]:
    from core.timeline import price_for

    prices = price_for(model)
    if prices is None:
        return None, None, None
    input_price, output_price, cached_price = prices
    uncached_prompt = max(0, prompt_tokens - cached_tokens)
    cost = (
        uncached_prompt * input_price
        + cached_tokens * cached_price
        + completion_tokens * output_price
    ) / 1_000_000
    uncached_cost = (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000
    return round(cost, 6), round(uncached_cost, 6), round(uncached_cost - cost, 6)


def _changed_files(workspace: Path) -> list[str]:
    if not (workspace / ".git").exists():
        return []
    result = _git(["-C", str(workspace), "status", "--porcelain"], capture=True)
    changed = []
    for line in result.splitlines():
        path = line[3:].strip()
        if path and not path.startswith(".arc/"):
            changed.append(path)
    return sorted(set(changed))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest_model_handoff(
    path: Path,
    node_id: str,
    *,
    cutoff_timestamp: str = "",
) -> str:
    if not path.is_file():
        return ""
    marker = f"[TestDrivenDeveloper][{node_id}] model-final>"
    continuation = f"[TestDrivenDeveloper][{node_id}] "
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    cutoff = None
    if cutoff_timestamp:
        try:
            cutoff = datetime.fromisoformat(cutoff_timestamp) + timedelta(seconds=1)
        except ValueError:
            cutoff = None
    matches: list[tuple[datetime | None, str]] = []
    for index, line in enumerate(lines):
        if marker not in line:
            continue
        timestamp = None
        timestamp_match = re.match(r"^\[([^\]]+)\]", line)
        if timestamp_match:
            try:
                timestamp = datetime.fromisoformat(timestamp_match.group(1))
            except ValueError:
                timestamp = None
        if cutoff is not None and timestamp is not None and timestamp > cutoff:
            continue
        parts = [line.split(marker, 1)[1].strip()]
        for following in lines[index + 1 :]:
            if continuation not in following:
                break
            body = following.split(continuation, 1)[1].strip()
            if body.startswith("agent call end:") or body.startswith("model-final>"):
                break
            parts.append(body)
        matches.append((timestamp, "\n\n".join(parts)))
    actionable = []
    for timestamp, text in matches:
        lowered = text.casefold()
        if any(
            marker in lowered
            for marker in (
                "latest_failure:",
                "latest failure:",
                "next_file:",
                "next file:",
                "next concrete edit target",
            )
        ):
            parsed = GlobalProjectMemory._parse_handoff_text(text)
            priority = GlobalProjectMemory._diagnostic_priority(
                observation=parsed.get("observation", ""),
                next_file=parsed.get("next_file", ""),
                next_action=parsed.get("next_action", ""),
            )
            actionable.append((priority, timestamp.timestamp() if timestamp else float("-inf"), text))
    if actionable:
        return max(actionable, key=lambda item: (item[0], item[1]))[2]
    nonterminal = [text for _, text in matches if text.strip().casefold() != "implemented"]
    return nonterminal[-1] if nonterminal else ""


def _reuse_dependencies(source: Path, output: Path) -> None:
    """Reuse ignored dependency trees without copying gigabytes per experiment."""
    for relative in ("node_modules", "frontend/node_modules", "backend/node_modules"):
        dependency_source = source / relative
        dependency_target = output / relative
        if dependency_source.is_dir() and not dependency_target.exists():
            dependency_target.parent.mkdir(parents=True, exist_ok=True)
            dependency_target.symlink_to(dependency_source, target_is_directory=True)


def _git(args: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return result.stdout if capture else ""
