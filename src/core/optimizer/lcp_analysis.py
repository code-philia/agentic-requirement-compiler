"""LCS analysis over agent trajectories reconstructed by ``timeline``.

A trajectory is one primary ARC agent invocation (node + phase + agent + layer
+ attempt). Its sequence contains model, tool, test, and nested-subagent events
inside that invocation. Comparisons operate on events, not prompt characters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from core.termui import col
from core.timeline import AgentCall, LogLine, Timeline, load_timeline


SEQUENCE_KINDS = {
    "model", "model-final", "tool-call", "test-run",
    "subagent-status", "subagent-messages", "subagent-output",
}


@dataclass(frozen=True)
class TrajectoryStep:
    epoch: float
    kind: str
    name: str
    symbol: str
    parameter_symbol: str
    raw_detail: str


@dataclass
class Trajectory:
    call_id: str
    node: str
    phase: str
    agent: str
    layer: str
    attempt: int
    start_epoch: float
    steps: list[TrajectoryStep]

    @property
    def symbols(self) -> list[str]:
        return [step.symbol for step in self.steps]

    @property
    def parameter_symbols(self) -> list[str]:
        return [step.parameter_symbol for step in self.steps if step.kind not in {"model", "model-final"}]

    @property
    def parameter_step_indices(self) -> list[int]:
        return [index for index, step in enumerate(self.steps)
                if step.kind not in {"model", "model-final"}]

    @property
    def label(self) -> str:
        agent = self.agent.replace("TestDrivenDeveloper", "TDD").replace("InterfaceDesigner", "IFace")
        attempt = f"#{self.attempt}" if self.attempt > 1 else ""
        return f"{agent}[{self.layer or '-'}]{attempt}@{self.node}.{self.phase}"


@dataclass
class PairResult:
    a: Trajectory
    b: Trajectory
    lcs: list[str]
    parameter_lcs: list[str]
    lcs_ratio: float
    parameter_lcs_ratio: float

    @property
    def kind(self) -> str:
        return "same-agent" if self.a.agent == self.b.agent else "cross-agent"


def _step_symbol(line: LogLine) -> str | None:
    """Reduce an event to its structural behavior symbol."""
    if line.kind in {"model", "model-final"}:
        return "model"
    if line.kind == "tool-call":
        return f"tool:{line.name or '?'}"
    if line.kind == "test-run":
        return "test-run"
    if line.kind == "subagent-status":
        return f"subagent:{line.name or '?'}:status"
    if line.kind == "subagent-messages":
        return f"subagent:{line.name or '?'}:messages"
    if line.kind == "subagent-output":
        return f"subagent:{line.name or '?'}:output"
    return None


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]


def _normalize_value(key: str, value: object) -> object:
    """Normalize tool arguments without retaining large file contents."""
    if isinstance(value, dict):
        return {name: _normalize_value(name, item) for name, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize_value(key, item) for item in value]
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        if normalized.startswith("/workspace/"):
            normalized = normalized[len("/workspace/"):]
        if key in {"content", "old_string", "new_string"}:
            return f"sha256:{_short_hash(normalized)}"
        return normalized
    return value


def _tool_arguments(detail: str) -> dict[str, object]:
    marker = "args="
    if marker not in detail:
        return {}
    raw = detail.split(marker, 1)[1].strip()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"raw_hash": _short_hash(raw)} if raw else {}
    return _normalize_value("", parsed) if isinstance(parsed, dict) else {"value": _normalize_value("", parsed)}


def _parameter_symbol(line: LogLine, structural_symbol: str) -> str:
    """Add stable, tool-specific parameters to a structural event symbol."""
    if line.kind == "tool-call":
        arguments = _tool_arguments(line.detail)
        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return f"{structural_symbol}:{encoded}"
    if line.kind == "test-run":
        return f"test-run:{line.name or '?'}"
    if line.kind.startswith("subagent-"):
        return structural_symbol
    return structural_symbol


def _events_for_call(timeline: Timeline, call: AgentCall) -> list[TrajectoryStep]:
    events: list[TrajectoryStep] = []
    for line in sorted(timeline.log_lines, key=lambda item: item.epoch):
        if not (call.start_epoch - 0.5 <= line.epoch <= call.end_epoch + 0.5):
            continue
        if line.agent != call.agent or line.kind not in SEQUENCE_KINDS:
            continue
        symbol = _step_symbol(line)
        if symbol is not None:
            events.append(TrajectoryStep(
                line.epoch, line.kind, line.name, symbol,
                _parameter_symbol(line, symbol), line.detail,
            ))
    return events


def load_trajectories(workspace: Path) -> list[Trajectory]:
    """Build one ordered event trajectory for every timeline AgentCall."""
    timeline = load_timeline(workspace)
    trajectories: list[Trajectory] = []
    attempts: dict[tuple[str, str, str, str], int] = {}
    for call in sorted(timeline.agent_calls, key=lambda item: item.start_epoch):
        key = (call.node, call.phase, call.agent, call.layer)
        attempts[key] = attempts.get(key, 0) + 1
        attempt = attempts[key]
        steps = _events_for_call(timeline, call)
        if steps:
            trajectories.append(Trajectory(
                call_id=":".join(key) + f"#{attempt}",
                node=call.node, phase=call.phase, agent=call.agent,
                layer=call.layer, attempt=attempt,
                start_epoch=call.start_epoch, steps=steps,
            ))
    return trajectories


def lcs_sequence(a: list[str], b: list[str]) -> list[str]:
    """Return one longest common subsequence while preserving event order."""
    lengths = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i, left in enumerate(a, 1):
        for j, right in enumerate(b, 1):
            if left == right:
                lengths[i][j] = lengths[i - 1][j - 1] + 1
            else:
                lengths[i][j] = max(lengths[i - 1][j], lengths[i][j - 1])

    result: list[str] = []
    i, j = len(a), len(b)
    while i and j:
        if a[i - 1] == b[j - 1]:
            result.append(a[i - 1])
            i -= 1
            j -= 1
        elif lengths[i - 1][j] >= lengths[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return list(reversed(result))


def lcs_alignment(a: list[str], b: list[str]) -> list[tuple[int, int]]:
    """Return the matched indices for one longest common subsequence."""
    lengths = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i, left in enumerate(a, 1):
        for j, right in enumerate(b, 1):
            lengths[i][j] = (lengths[i - 1][j - 1] + 1 if left == right
                             else max(lengths[i - 1][j], lengths[i][j - 1]))
    matches: list[tuple[int, int]] = []
    i, j = len(a), len(b)
    while i and j:
        if a[i - 1] == b[j - 1]:
            matches.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif lengths[i - 1][j] >= lengths[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return list(reversed(matches))


def parameter_alignment(a: Trajectory, b: Trajectory) -> list[tuple[int, int]]:
    """Parameter LCS mapped back to indices in the complete trajectories."""
    filtered = lcs_alignment(a.parameter_symbols, b.parameter_symbols)
    left_indices = a.parameter_step_indices
    right_indices = b.parameter_step_indices
    return [(left_indices[left], right_indices[right]) for left, right in filtered]


def analysis_data(trajectories: list[Trajectory], results: list[PairResult]) -> dict[str, object]:
    """Serializable complete analysis for the local trace inspector."""
    trajectory_data = []
    by_id: dict[str, int] = {}
    for index, trajectory in enumerate(trajectories):
        by_id[trajectory.call_id] = index
        trajectory_data.append({
            "id": trajectory.call_id, "label": trajectory.label,
            "node": trajectory.node, "phase": trajectory.phase,
            "agent": trajectory.agent, "layer": trajectory.layer,
            "attempt": trajectory.attempt, "start_epoch": trajectory.start_epoch,
            "steps": [
                {"epoch": step.epoch, "kind": step.kind, "name": step.name,
                 "symbol": step.symbol, "parameter_symbol": step.parameter_symbol,
                 "raw_detail": step.raw_detail}
                for step in trajectory.steps
            ],
        })
    pair_data = []
    for result in results:
        pair_data.append({
            "id": f"{result.a.call_id}__{result.b.call_id}",
            "left": by_id[result.a.call_id], "right": by_id[result.b.call_id],
            "kind": result.kind, "shape_ratio": result.lcs_ratio,
            "parameter_ratio": result.parameter_lcs_ratio,
            "shape_alignment": lcs_alignment(result.a.symbols, result.b.symbols),
            "parameter_alignment": parameter_alignment(result.a, result.b),
        })
    return {"trajectories": trajectory_data, "pairs": pair_data}


def analyze(trajectories: list[Trajectory], *, cross_agent: bool = False) -> list[PairResult]:
    """Compare each unique compatible pair; same-agent is the default."""
    results: list[PairResult] = []
    for i, left in enumerate(trajectories):
        for right in trajectories[i + 1:]:
            if not cross_agent and left.agent != right.agent:
                continue
            shape_shorter = min(len(left.steps), len(right.steps))
            parameter_shorter = min(len(left.parameter_symbols), len(right.parameter_symbols))
            if not shape_shorter:
                continue
            common = lcs_sequence(left.symbols, right.symbols)
            parameter_common = lcs_sequence(left.parameter_symbols, right.parameter_symbols)
            results.append(PairResult(
                left, right, common, parameter_common,
                len(common) / shape_shorter,
                len(parameter_common) / parameter_shorter if parameter_shorter else 0.0,
            ))
    return sorted(
        results,
        key=lambda item: (item.parameter_lcs_ratio, item.lcs_ratio, len(item.parameter_lcs)),
        reverse=True,
    )


def _matches(value: str, requested: str | None) -> bool:
    return requested is None or value.casefold() == requested.casefold()


def filter_trajectories(
    trajectories: list[Trajectory], *, node: str | None, phase: str | None,
    agent: str | None, layer: str | None,
) -> list[Trajectory]:
    return [trajectory for trajectory in trajectories
            if _matches(trajectory.node, node)
            and _matches(trajectory.phase, phase)
            and _matches(trajectory.agent, agent)
            and _matches(trajectory.layer, layer)]


def _pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


def _sample(sequence: list[str], limit: int = 8) -> str:
    text = " → ".join(sequence[:limit]) if sequence else "—"
    return text + (" → …" if len(sequence) > limit else "")


def render(results: list[PairResult], trajectories: list[Trajectory], top: int, min_pct: float) -> str:
    out = [col(" ARC agent trajectory LCS analysis", bold=True, fg="blue")]
    out.append(f"  {len(trajectories)} agent trajectories · "
               f"{sum(len(t.steps) for t in trajectories):,} normalized events")
    for trajectory in trajectories:
        out.append(f"   {trajectory.label}: {len(trajectory.steps)} events")

    shown = [result for result in results if result.parameter_lcs_ratio * 100 >= min_pct][:top]
    if not shown:
        out.extend(["", col("  no compatible pair meets the parameter-LCS threshold", fg="gray")])
        return "\n".join(out)

    out.extend(["", col(f"  top {len(shown)} pairs (parameter LCS >= {min_pct:.0f}% of shorter trajectory)",
                        bold=True, fg="gray")])
    out.append(col(f"  {'pair'.ljust(57)} {'shape':>7} {'params':>7} {'events':>11}  kind", bold=True))
    for result in shown:
        pair = f"{result.a.label} <-> {result.b.label}"
        events = f"{len(result.a.steps)}/{len(result.b.steps)}"
        out.append(f"  {pair[:56].ljust(57)} {_pct(result.lcs_ratio):>7} "
                   f"{_pct(result.parameter_lcs_ratio):>7} {events:>11}  {result.kind}")

    best = shown[0]
    out.extend(["", col("  best pair", bold=True, fg="gray"),
                f"   structural common : {_sample(best.lcs)}",
                f"   parameter common  : {_sample(best.parameter_lcs)}"])
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="LCS similarity across ARC agent trajectories")
    parser.add_argument("workspace", type=Path, help="ARC output directory containing .arc/")
    parser.add_argument("--node", help="exact requirement node, for example REQ-1.1")
    parser.add_argument("--phase", help="exact phase, for example implement")
    parser.add_argument("--agent", help="exact primary agent name")
    parser.add_argument("--layer", help="exact layer, for example Integration")
    parser.add_argument("--cross-agent", action="store_true", help="also compare different primary agent types")
    parser.add_argument("--json", action="store_true", help="emit complete machine-readable analysis")
    parser.add_argument("--top", type=int, default=15, help="maximum pairs to display")
    parser.add_argument("--min-pct", type=float, default=20.0,
                        help="minimum parameter-aware LCS percentage of the shorter trajectory")
    args = parser.parse_args()

    trajectories = filter_trajectories(
        load_trajectories(args.workspace.resolve()),
        node=args.node, phase=args.phase, agent=args.agent, layer=args.layer,
    )
    if not trajectories:
        print("no matching agent trajectories found", file=sys.stderr)
        return 1
    results = analyze(trajectories, cross_agent=args.cross_agent)
    if args.json:
        print(json.dumps(analysis_data(trajectories, results), ensure_ascii=False))
    else:
        print(render(results, trajectories, args.top, args.min_pct))
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
