"""core.timeline — step-through timeline and bottleneck analysis for an ARC workspace.

Run it as an ARC subcommand:
    arc timeline <output-dir>

It reconstructs the compilation history from the workspace artifacts:

  .arc/runner-events.jsonl   — the full event stream (runner_state,
                               requirement_state transitions, interface/test
                               upserts, git signals) with UTC timestamps
  .arc/debug.log             — agent call start/end lines with duration_ms,
                               plus visual-analysis steps

Two screens, plus drill-down:

  [1] Timeline  — time-axis gantt of phase segments per requirement node (a
                  "now" marker while the run is live) and bottleneck rankings:
                  slowest phase segments, total time per agent, idle gaps.
                  Select a segment (j/k) and press Enter to drill in.
  [2] Steps     — step through every event one at a time (←/→), autoplay with
                  space, full untruncated JSON of the current event, and a
                  replay tally (interfaces/tests/commits so far, runner state)

  Drill-down — press Enter on a phase segment:
    phase view   agent calls inside the phase: per-call duration, tool-execution
                 vs model-thinking time, token usage per call and per layer,
                 and idle gaps
    call view    every tool call inside one agent call with its own duration
                 (read/edit files, run_tests, ...), plus prompt/completion/
                 total tokens and LLM-call count when the run recorded them;
                 ↑↓/j k scroll the tool list, ←/→ switch between calls.
                 When the run was compiled with ARC_DEBUG_AGENT_TRACE=1 the
                 prompt the call received is shown at the bottom: the assembled
                 instruction plus the model inputs (system template + human
                 message per round-trip, deduplicated).

Token usage is captured at the model adapter on every LLM call and written to
debug.log as `agent usage: ...` lines; runs compiled by older ARC builds have
no token data and show "—" with a note.

Cost estimation: each call's cost is derived from prompt/completion/cached
tokens times the model's per-1M prices (built-in table for common models).
Override prices with the env vars ARC_INPUT_PRICE_PER_1M, ARC_OUTPUT_PRICE_PER_1M,
ARC_CACHED_INPUT_PRICE_PER_1M (USD per 1M tokens); calls recorded before the
model field existed fall back to the $MODEL env var. The call detail shows the
call's cost, its cumulative share of the run, and its percentage.

Keybindings:
  1 / 2           switch screens (timeline / steps)
  j k / arrows    move selection (timeline segments, drill calls)
  Enter           drill into a phase / open a call
  Esc             back one level
  ← → / h l       step one event (steps screen)
  g / G           jump to first / last event
  space           play / pause stepping
  p               pause / resume live polling
  q               quit

If stdout is not a terminal it prints one static snapshot instead.
"""

from __future__ import annotations

import argparse
import atexit
import calendar
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.termui import (
    col,
    hide_cursor,
    phase_color,
    set_color,
    show_cursor,
    terminal_size,
    wrap_text,
)

# ---------------------------------------------------------------------------
# Timeline data model
# ---------------------------------------------------------------------------

STEP_PHASES = ("design", "implement", "test")


@dataclass
class Step:
    """One raw event from runner-events.jsonl with a UTC epoch."""

    line: int
    ts: str
    epoch: float
    data: dict


@dataclass
class Segment:
    """A node phase (design/implement/test) from running -> completed/failed."""

    node: str
    phase: str
    status: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class AgentCall:
    """One agent invocation measured from debug.log duration_ms."""

    node: str
    phase: str
    agent: str
    layer: str
    duration: float
    start_epoch: float
    end_epoch: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    llm_calls: int = 0
    model: str = ""
    prompt_text: str = ""
    model_inputs: list = field(default_factory=list)

    @property
    def has_usage(self) -> bool:
        return self.total_tokens > 0

    @property
    def cache_rate(self) -> float:
        """Fraction of prompt tokens served from the provider cache."""
        return self.cached_tokens / self.prompt_tokens * 100 if self.prompt_tokens else 0.0

    @property
    def cost(self) -> float | None:
        """USD cost of this call, or None when no usage/price is available."""
        if not self.has_usage:
            return None
        prices = price_for(self.model)
        if prices is None:
            return None
        inp, outp, cached_in = prices
        fresh_prompt = max(0, self.prompt_tokens - self.cached_tokens)
        return (
            fresh_prompt * inp + self.cached_tokens * cached_in + self.completion_tokens * outp
        ) / 1_000_000


@dataclass
class LogLine:
    """One parsed debug.log line."""

    epoch: float
    agent: str
    node: str
    kind: str      # tool-call | tool-result | model-final | test-run | other
    name: str
    detail: str


@dataclass
class ToolStep:
    """One tool call inside an agent call, with an estimated duration."""

    name: str
    kind: str
    detail: str
    start: float
    duration: float


@dataclass
class VisualStep:
    """One visual-reference analysis measured from debug.log start/end lines."""

    node: str
    duration: float
    end_epoch: float


@dataclass
class Timeline:
    steps: list[Step] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    agent_calls: list[AgentCall] = field(default_factory=list)
    visual_steps: list[VisualStep] = field(default_factory=list)
    log_lines: list[LogLine] = field(default_factory=list)
    runner_state: str | None = None
    runner_start: float | None = None
    runner_end: float | None = None
    workspace: str = ""

    @property
    def t0(self) -> float:
        return self.steps[0].epoch if self.steps else 0.0

    @property
    def t1(self) -> float:
        if self.runner_end is not None:
            return self.runner_end
        if self.steps:
            return max(self.steps[-1].epoch, time.time())
        return 0.0

    @property
    def elapsed(self) -> float:
        return max(0.0, self.t1 - self.t0)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"^\[([^\]]+)\]")
_START_RE = re.compile(r"agent call start: thread_id=(\S+)")
_END_RE = re.compile(r"agent call end: duration_ms=([0-9.]+)")
_VSTART_RE = re.compile(r"Analyzing visual element: (\S+)")
_VEND_RE = re.compile(r"Stored \d+ visual references for (\S+)")
_TOOLCALL_RE = re.compile(r"tool-call>\s*(\S+)")
_TOOLRESULT_RE = re.compile(r"tool-result>\s*(\S+)")
_TESTRUN_RE = re.compile(r"System test execution \(([^)]+)\):\s*(\S+)")
_USAGE_RE = re.compile(
    r"agent usage: prompt_tokens=(\d+) completion_tokens=(\d+) total_tokens=(\d+)"
    r"(?: cached_tokens=(\d+))?(?: llm_calls=(\d+))?(?: model=(\S+))?"
)
_PROMPT_RE = re.compile(r"agent prompt: thread_id=(\S+)")
_MODEL_INPUT_RE = re.compile(r"model input:")
_SUBAGENT_STATUS_RE = re.compile(r"subagent>\s*(\S+)\s+status=(\S+)")
_SUBAGENT_MESSAGES_RE = re.compile(r"subagent messages \(([^)]+)\):")
_SUBAGENT_OUTPUT_RE = re.compile(r"subagent output \(([^)]+)\):")


def _line_kind_and_name(rest: str) -> tuple[str, str, str]:
    """Classify a debug.log line (after its timestamp prefix)."""
    name = ""
    detail = ""
    if "tool-call>" in rest:
        kind = "tool-call"
        m2 = _TOOLCALL_RE.search(rest)
        if m2:
            name = m2.group(1)
            detail = rest[m2.end():].strip()
        return kind, name, detail
    if "tool-result>" in rest:
        kind = "tool-result"
        m2 = _TOOLRESULT_RE.search(rest)
        if m2:
            name = m2.group(1)
        return kind, name, detail
    if "model-final>" in rest:
        return "model-final", "", rest.split("model-final>", 1)[1].strip()
    if rest.lstrip().startswith("model>"):
        return "model", "", rest.split("model>", 1)[1].strip()
    m2 = _SUBAGENT_STATUS_RE.search(rest)
    if m2:
        return "subagent-status", m2.group(1), m2.group(2)
    m2 = _SUBAGENT_MESSAGES_RE.search(rest)
    if m2:
        return "subagent-messages", m2.group(1), rest[m2.end():].strip()
    m2 = _SUBAGENT_OUTPUT_RE.search(rest)
    if m2:
        return "subagent-output", m2.group(1), rest[m2.end():].strip()
    m2 = _USAGE_RE.search(rest)
    if m2:
        return "usage", "agent-usage", " ".join(rest.split())
    m2 = _PROMPT_RE.match(rest.lstrip())
    if m2:
        return "prompt", m2.group(1), rest
    if _MODEL_INPUT_RE.match(rest.lstrip()):
        return "model-input", "model-input", rest
    m2 = _TESTRUN_RE.search(rest)
    if m2:
        return "test-run", f"{m2.group(1)} {m2.group(2)}", ""
    if "agent call start" in rest:
        return "other", "agent-call-start", ""
    if "agent call end" in rest:
        return "other", "agent-call-end", ""
    return "other", "", ""


def _node_from_line(raw: str) -> str:
    """Log lines are '[ts] [agent][node] ...' (or '[ts] [agent][node][status]')."""
    brackets = re.findall(r"\[([^\]]+)\]", raw)
    return brackets[2] if len(brackets) > 2 else "?"


def parse_event_ts(ts: str) -> float:
    """Event timestamps are UTC without offset ("YYYY-MM-DD HH:MM:SS")."""
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


def parse_log_ts(ts: str) -> float:
    """debug.log timestamps carry a local offset ("2026-08-20T17:16:24.894+07:00")."""
    try:
        return datetime.fromisoformat(ts).astimezone(timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0


def read_events(path: Path) -> list[Step]:
    steps: list[Step] = []
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return steps
    prev_epoch = 0.0
    for line_no, raw in enumerate(text.splitlines()):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        epoch = parse_event_ts(data.get("timestamp"))
        if epoch <= 0:
            epoch = prev_epoch
        else:
            prev_epoch = epoch
        steps.append(Step(line_no, str(data.get("timestamp") or ""), epoch, data))
    return steps


def build_segments(steps: list[Step], t1: float) -> list[Segment]:
    segments: list[Segment] = []
    open_seg: dict[tuple[str, str], float] = {}
    for ev in steps:
        if ev.data.get("type") != "requirement_state":
            continue
        node = ev.data.get("node_id") or ""
        phase = str(ev.data.get("phase") or "").lower()
        status = str(ev.data.get("status") or "").lower()
        if not node or phase not in STEP_PHASES:
            continue
        if status == "running":
            open_seg[(node, phase)] = ev.epoch
        elif status in ("completed", "failed", "passed"):
            start = open_seg.pop((node, phase), ev.epoch)
            segments.append(Segment(node, phase, status, start, ev.epoch))
    # Still-running phases get an open-ended segment up to the timeline end.
    for (node, phase), start in open_seg.items():
        segments.append(Segment(node, phase, "running", start, t1))
    return segments


def parse_agent_calls(log_path: Path) -> tuple[list[AgentCall], list[VisualStep]]:
    calls: list[AgentCall] = []
    visual: list[VisualStep] = []
    try:
        text = log_path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return calls, visual

    pending: list[tuple[str, str, str, str, float]] = []
    visual_start: dict[str, float] = {}
    for raw in text.splitlines():
        m = _TS_RE.match(raw)
        if not m:
            continue
        epoch = parse_log_ts(m.group(1))
        if _START_RE.search(raw):
            m2 = _START_RE.search(raw)
            thread = m2.group(1).split(":")
            node = thread[0]
            phase = thread[1].lower() if len(thread) > 1 else "?"
            agent = thread[2] if len(thread) >= 3 else (thread[-1] if thread else "?")
            layer = ":".join(thread[3:])
            pending.append((node, phase, agent, layer, epoch))
        m2 = _END_RE.search(raw)
        if m2:
            duration = float(m2.group(1)) / 1000.0
            # An `agent call end` belongs to the most recent start BEFORE it.
            # FIFO pairing breaks when a process is killed mid-call and leaves
            # an orphaned start behind (it would steal every later pairing).
            match_idx = None
            for idx in range(len(pending) - 1, -1, -1):
                if pending[idx][4] <= epoch:
                    match_idx = idx
                    break
            if match_idx is not None:
                node, phase, agent, layer, start_epoch = pending.pop(match_idx)
            else:
                node, phase, agent, layer, start_epoch = "?", "?", "?", "", epoch - duration
            calls.append(AgentCall(node, phase, agent, layer, duration, start_epoch, epoch))
        m2 = _VSTART_RE.search(raw)
        if m2:
            node = _node_from_line(raw)
            visual_start[node] = epoch
        m2 = _VEND_RE.search(raw)
        if m2:
            node = _node_from_line(raw)
            start = visual_start.pop(node, None)
            if start is not None:
                visual.append(VisualStep(node, max(0.0, epoch - start), epoch))
    # Orphaned starts (no matching end — process killed mid-call) are dropped.
    return calls, visual


def parse_log_lines(path: Path) -> list[LogLine]:
    """Parse every timestamped debug.log line into a LogLine.

    Multiline `agent prompt:` blocks (the full prompt is split across prefixed
    log lines) are reassembled into a single LogLine with the full text.
    """
    lines: list[LogLine] = []
    pending_prompt: LogLine | None = None
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return lines
    for raw in text.splitlines():
        m = _TS_RE.match(raw)
        if not m:
            continue
        epoch = parse_log_ts(m.group(1))
        if epoch <= 0:
            continue
        brackets = re.findall(r"\[([^\]]+)\]", raw)
        agent = brackets[1] if len(brackets) > 1 else ""
        node = brackets[2] if len(brackets) > 2 else ""
        rest = raw[m.end():].lstrip()
        if rest.startswith("["):
            # Drop the [agent][node][status] prefix, keeping the message content.
            parts = rest.split("] ", 1)
            if len(parts) > 1:
                rest = parts[1]
        kind, name, detail = _line_kind_and_name(rest)
        if kind in ("prompt", "model-input"):
            # Start of a multiline block: keep the marker line and collect the
            # continuation lines below.
            if pending_prompt is not None:
                lines.append(pending_prompt)
            pending_prompt = LogLine(epoch, agent, node, kind, name, rest)
            continue
        if pending_prompt is not None:
            # Continuation line of the open block (no recognized marker).
            if kind == "other" and not name:
                pending_prompt.detail += "\n" + rest.strip()
                continue
            lines.append(pending_prompt)
            pending_prompt = None
        lines.append(LogLine(epoch, agent, node, kind, name, detail))
    if pending_prompt is not None:
        lines.append(pending_prompt)
    return lines


def attach_usage(tl: Timeline) -> None:
    """Attribute `agent usage:` log lines to the enclosing agent call."""
    for line in tl.log_lines:
        if line.kind != "usage":
            continue
        m = _USAGE_RE.search(line.detail)
        if not m:
            continue
        prompt, completion, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
        cached = int(m.group(4) or 0)
        calls = int(m.group(5) or 1)
        model = m.group(6) or ""
        for call in tl.agent_calls:
            if call.agent == line.agent and call.start_epoch - 0.5 <= line.epoch <= call.end_epoch + 0.5:
                call.prompt_tokens += prompt
                call.completion_tokens += completion
                call.total_tokens += total
                call.cached_tokens += cached
                call.llm_calls += calls
                if model and not call.model:
                    call.model = model
                break
    # Calls recorded before the model field existed fall back to the configured
    # model (the same model the run was compiled with).
    env_model = os.environ.get("MODEL", "").strip()
    if env_model:
        for call in tl.agent_calls:
            if not call.model:
                call.model = env_model
    # Attach the full prompt block (when logged under ARC_DEBUG_AGENT_TRACE=1).
    for line in tl.log_lines:
        if line.kind not in ("prompt", "model-input"):
            continue
        for call in tl.agent_calls:
            if call.agent == line.agent and call.start_epoch - 0.5 <= line.epoch <= call.end_epoch + 0.5:
                if line.kind == "prompt":
                    call.prompt_text = line.detail
                else:
                    call.model_inputs.append(line.detail)
                break


# ---------------------------------------------------------------------------
# Cost estimation (per-1M-token USD)
# ---------------------------------------------------------------------------

# Known OpenAI per-1M prices: (input, output, cached_input). Cache price
# missing for a few legacy models — falls back to the input price.
_MODEL_PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-5.4": (2.50, 15.00, 0.25),
    "gpt-4o": (2.50, 10.00, 1.25),
    "gpt-4o-mini": (0.15, 0.60, 0.075),
    "gpt-4.1": (2.00, 8.00, 0.50),
    "gpt-4.1-mini": (0.40, 1.60, 0.10),
    "gpt-4.1-nano": (0.10, 0.40, 0.025),
    "gpt-4-turbo": (10.00, 30.00, 10.00),
    "gpt-4": (30.00, 60.00, 30.00),
    "o4-mini": (1.10, 4.40, 0.55),
    "o3": (2.00, 8.00, 0.50),
    "o3-mini": (1.10, 4.40, 0.55),
    "o1": (15.00, 60.00, 7.50),
    "o1-mini": (1.10, 4.40, 0.55),
}


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def price_for(model: str) -> tuple[float, float, float] | None:
    """Per-1M USD (input, output, cached input) for a model, or None."""
    name = (model or "").strip()
    if not name:
        return None
    inp = _env_float("ARC_INPUT_PRICE_PER_1M")
    outp = _env_float("ARC_OUTPUT_PRICE_PER_1M")
    if inp is not None and outp is not None:
        cached = _env_float("ARC_CACHED_INPUT_PRICE_PER_1M") or inp
        return (inp, outp, cached)
    for prefix, prices in _MODEL_PRICES.items():
        if name.startswith(prefix):
            return prices
    return None


def fmt_cost(cost: float | None) -> str:
    if cost is None:
        return "—"
    if cost >= 100:
        return f"${cost:,.0f}"
    if cost >= 1:
        return f"${cost:.2f}"
    if cost >= 0.01:
        return f"${cost:.3f}"
    return f"${cost:.4f}"


def total_cost(tl: Timeline) -> float:
    return sum(c.cost for c in tl.agent_calls if c.cost is not None)


def cumulative_cost(tl: Timeline, call: AgentCall) -> float:
    """Total cost of all calls up to and including `call`, in run order."""
    ordered = sorted(tl.agent_calls, key=lambda c: (c.start_epoch, c.end_epoch))
    total = 0.0
    for c in ordered:
        total += c.cost or 0.0
        if c is call:
            break
    return total


def phase_calls(tl: Timeline, node: str, phase: str) -> list[AgentCall]:
    """Agent calls inside a phase, in execution order."""
    return [c for c in tl.agent_calls if c.node == node and c.phase == phase]


_LAYER_ORDER = ("Unit", "Integration", "E2E")


def group_calls(tl: Timeline, node: str, phase: str) -> list[dict]:
    """Group a phase's calls by layer (falling back to agent), with totals.

    Returns groups ordered by the TDD layer order Unit -> Integration -> E2E
    for known layers, then remaining groups by first appearance.
    """
    calls = phase_calls(tl, node, phase)
    groups: list[dict] = []
    index: dict[str, dict] = {}
    for c in calls:
        key = c.layer or c.agent or "?"
        g = index.get(key)
        if g is None:
            g = {"layer": key, "calls": [], "total": 0.0, "tools_total": 0.0,
                 "thinking_total": 0.0, "tokens": 0, "prompt": 0, "cached": 0,
                 "first": len(groups)}
            index[key] = g
            groups.append(g)
        g["calls"].append(c)
        g["total"] += c.duration
        g["tokens"] += c.total_tokens
        g["prompt"] += c.prompt_tokens
        g["cached"] += c.cached_tokens
        exec_total, thinking, _ = call_breakdown(tl, c)
        g["tools_total"] += exec_total
        g["thinking_total"] += thinking
    known = {name: i for i, name in enumerate(_LAYER_ORDER)}
    groups.sort(key=lambda g: (known.get(g["layer"], 10), g["first"]))
    return groups


def call_tools(tl: Timeline, call: AgentCall) -> list[ToolStep]:
    """Tool calls inside an agent call; duration = tool-call -> next boundary.

    Boundaries are exactly tool-call / tool-result / model-final lines; all
    other lines (test-run notices, skill loads, usage counters) are noise.
    """
    window = [
        l for l in tl.log_lines
        if call.start_epoch - 0.5 <= l.epoch <= call.end_epoch + 0.5
        and (l.agent == call.agent or l.kind == "test-run")
    ]
    steps: list[ToolStep] = []
    open_start: float | None = None
    open_name = ""
    open_kind = ""
    open_detail = ""
    for l in sorted(window, key=lambda x: x.epoch):
        if l.kind == "tool-call":
            if open_start is not None:
                steps.append(ToolStep(open_name, open_kind, open_detail,
                                      open_start, l.epoch - open_start))
            open_start = l.epoch
            open_name = l.name
            open_kind = l.kind
            open_detail = l.detail
        elif l.kind in ("tool-result", "model-final"):
            if open_start is not None:
                steps.append(ToolStep(open_name, open_kind, open_detail,
                                      open_start, l.epoch - open_start))
                open_start = None
                open_name = ""
                open_kind = ""
                open_detail = ""
    if open_start is not None:
        steps.append(ToolStep(open_name, open_kind, open_detail,
                              open_start, max(0.0, call.end_epoch - open_start)))
    return steps


def call_breakdown(tl: Timeline, call: AgentCall) -> tuple[float, float, list[ToolStep]]:
    """(tool execution total, model thinking total, tool steps) for a call."""
    tools = call_tools(tl, call)
    exec_total = sum(t.duration for t in tools)
    return exec_total, max(0.0, call.duration - exec_total), tools


def phase_gaps(tl: Timeline, node: str, phase: str) -> list[tuple[float, str]]:
    """Idle pauses between consecutive agent calls inside a phase."""
    calls = phase_calls(tl, node, phase)
    gaps: list[tuple[float, str]] = []
    for prev, nxt in zip(calls, calls[1:]):
        gap = nxt.start_epoch - prev.end_epoch
        if gap > 5.0:
            gaps.append((gap, f"{prev.agent} {prev.layer or ''} → {nxt.agent} {nxt.layer or ''}".strip()))
    return sorted(gaps, reverse=True)[:5]


def load_timeline(workspace: Path) -> Timeline:
    arc = workspace / ".arc"
    steps = read_events(arc / "runner-events.jsonl")
    tl = Timeline(steps=steps, workspace=str(workspace))

    for ev in steps:
        if ev.data.get("type") != "runner_state":
            continue
        state = ev.data.get("state")
        tl.runner_state = state
        if state == "running" and tl.runner_start is None:
            tl.runner_start = ev.epoch
        elif state in ("completed", "failed") :
            tl.runner_end = ev.epoch

    tl.segments = build_segments(steps, tl.t1)
    tl.agent_calls, tl.visual_steps = parse_agent_calls(arc / "debug.log")
    tl.log_lines = parse_log_lines(arc / "debug.log")
    attach_usage(tl)
    return tl


# ---------------------------------------------------------------------------
# Analysis / bottleneck helpers
# ---------------------------------------------------------------------------

def _seg_depth(node: str) -> int:
    """Hierarchy depth: ROOT=0, REQ-1=1, REQ-1.1=2, ..."""
    return node.count(".") + (0 if node == "ROOT" else 1)


def ordered_segments(tl: Timeline) -> list[Segment]:
    """Canonical segment order: chronological, with parents before children
    (ROOT first) when start times tie."""
    return sorted(
        tl.segments,
        key=lambda s: (s.start, _seg_depth(s.node), s.node, s.phase),
    )

def fmt_dur(seconds: float) -> str:
    s = max(0.0, seconds)
    if s < 10:
        return f"{s:.1f}s"
    if s < 60:
        return f"{int(s)}s"
    minutes = int(s // 60)
    if minutes < 60:
        return f"{minutes}m{int(s % 60):02d}s"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def fmt_tokens(count: int) -> str:
    if count <= 0:
        return "—"
    if count < 1000:
        return f"{count} tok"
    if count < 1_000_000:
        return f"{count / 1000:.1f}k tok"
    return f"{count / 1_000_000:.2f}M tok"


def fmt_ts(epoch: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def agent_totals(tl: Timeline) -> list[dict]:
    totals: dict[str, dict] = {}
    for c in tl.agent_calls:
        entry = totals.setdefault(c.agent, {"total": 0.0, "count": 0, "nodes": set(),
                                            "tokens": 0, "prompt": 0, "cached": 0})
        entry["total"] += c.duration
        entry["count"] += 1
        entry["tokens"] += c.total_tokens
        entry["prompt"] += c.prompt_tokens
        entry["cached"] += c.cached_tokens
        entry["nodes"].add(c.node)
    return sorted(
        ({"agent": a, "total": v["total"], "count": v["count"], "nodes": sorted(v["nodes"]),
          "tokens": v["tokens"], "prompt": v["prompt"], "cached": v["cached"]}
         for a, v in totals.items()),
        key=lambda e: e["total"],
        reverse=True,
    )


def total_tokens(tl: Timeline) -> int:
    return sum(c.total_tokens for c in tl.agent_calls)


def phase_tokens(tl: Timeline, seg: Segment) -> int:
    return sum(c.total_tokens for c in phase_calls(tl, seg.node, seg.phase))


def phase_prompt(tl: Timeline, seg: Segment) -> int:
    return sum(c.prompt_tokens for c in phase_calls(tl, seg.node, seg.phase))


def phase_cached(tl: Timeline, seg: Segment) -> int:
    return sum(c.cached_tokens for c in phase_calls(tl, seg.node, seg.phase))


def idle_gaps(tl: Timeline) -> list[tuple[float, str]]:
    """Longest pauses between consecutive phase segments (overhead windows)."""
    ordered = ordered_segments(tl)
    gaps: list[tuple[float, str]] = []
    for prev, nxt in zip(ordered, ordered[1:]):
        gap = nxt.start - prev.end
        if gap > 5.0:
            gaps.append((gap, f"{prev.node} {prev.phase} → {nxt.node} {nxt.phase}"))
    return sorted(gaps, reverse=True)[:5]


# ---------------------------------------------------------------------------
# Rendering — timeline screen
# ---------------------------------------------------------------------------

def header_lines(tl: Timeline, paused: bool) -> list[str]:
    st = (tl.runner_state or "NO RUN").upper()
    fg, bold = {"RUNNING": ("yellow", True), "COMPLETED": ("green", False),
                "FAILED": ("red", True)}.get(st, (None, False))
    badge = col(f" [{st}] ", fg=fg, bold=True)
    ifaces = sum(1 for e in tl.steps if e.data.get("type") == "interface_upsert")
    tests = sum(1 for e in tl.steps if e.data.get("type") == "test_upsert")
    tokens = total_tokens(tl)
    tokens_str = f" · {fmt_tokens(tokens)}" if tokens else ""
    paused_tag = col(" PAUSED ", fg="yellow", bold=True) if paused else ""
    return [
        col(" ARC timeline", bold=True, fg="blue") + badge
        + f" {len(tl.steps)} events · elapsed {fmt_dur(tl.elapsed)}"
        + f" · {ifaces} ifaces · {tests} tests{tokens_str}" + paused_tag,
        col("  " + tl.workspace, fg="gray"),
    ]


def render_gantt(tl: Timeline, width: int) -> list[tuple[str, int | None]]:
    """Returns (line, segment_index|None) so rows are selectable for drill-down."""
    if not tl.steps:
        return [(col("  no events recorded yet", fg="gray"), None)]
    span = max(tl.elapsed, 1e-6)
    label_w = 22
    bar_w = max(12, min(width - label_w - 34, 90))
    now = time.time() if tl.runner_state == "running" else None
    sorted_segs = ordered_segments(tl)

    rows: list[tuple[str, int | None]] = [(
        col("  time ", fg="gray") + "─" * bar_w + "►"
        + col(f"  {fmt_ts(tl.t0)} → {fmt_ts(tl.t1)} (+{fmt_dur(tl.elapsed)})", fg="gray"),
        None,
    )]
    for idx, seg in enumerate(sorted_segs):
        # Clamp both ends so a phase that lands exactly on the last axis cell
        # (e.g. 0s phases at run end) still draws a visible marker and the
        # sequence stays visually continuous.
        c0 = min(int((seg.start - tl.t0) / span * bar_w), bar_w - 1)
        c1 = min(max(c0, int((seg.end - tl.t0) / span * bar_w)), bar_w - 1)
        cells = ["░"] * bar_w
        for i in range(c0, c1 + 1):
            cells[i] = "█"
        if now is not None:
            nc = int((now - tl.t0) / span * bar_w)
            if 0 <= nc < bar_w:
                cells[nc] = "│"
        fg = phase_color(seg.phase)
        if seg.status == "failed":
            fg = "red"
        bar = col("".join(cells), fg=fg, bold=(seg.status == "running"))
        status = seg.status if seg.status == "running" else ""
        label = f"{seg.node} {seg.phase}"
        tok = phase_tokens(tl, seg)
        tokens_str = f" · {fmt_tokens(tok)}" if tok else ""
        rows.append((
            f"  {label.ljust(label_w)} {bar} "
            f"{col(fmt_dur(seg.duration).rjust(8), bold=True)}"
            f"{'  (' + status + ')' if status else ''}{tokens_str}",
            idx,
        ))
    if now is not None:
        rows.append((col(f"  {' ' * label_w} {' ' * (int((now - tl.t0) / span * bar_w))}│  = now", fg="gray"), None))
    rows.append((col("  legend: design=blue implement=yellow test=green failed=red · Enter: drill in", fg="gray"), None))
    return rows


def render_bottlenecks(tl: Timeline, width: int) -> list[tuple[str, int | None]]:
    out: list[tuple[str, int | None]] = [(col("  bottlenecks", bold=True, fg="gray"), None)]
    if not tl.segments:
        out.append((col("    (no phase segments yet)", fg="gray"), None))
        return out
    sorted_segs = ordered_segments(tl)
    tok_total = total_tokens(tl)
    any_cache = any(c.cached_tokens > 0 for c in tl.agent_calls)
    bar_w = max(6, min(30, width // 3))

    def cache_col(cached: int, prompt: int) -> str:
        if not any_cache:
            return ""
        if prompt <= 0:
            return "cache —".rjust(12)
        return f"cache {cached / prompt * 100:5.1f}%".rjust(12)

    def tok_col(count: int, share: float | None = None) -> str:
        if not count:
            return "—".rjust(19)
        if share is None:
            text = fmt_tokens(count).rjust(11)
        else:
            text = f"{fmt_tokens(count).rjust(11)} ({share:5.1f}%)"
        return col(text, bold=True)

    # slowest phases by duration
    top = sorted(tl.segments, key=lambda s: s.duration, reverse=True)[:8]
    for rank, seg in enumerate(top, 1):
        pct = seg.duration / tl.elapsed * 100 if tl.elapsed > 0 else 0.0
        filled = round(bar_w * min(pct, 100) / 100)
        bar = col("█" * filled + "░" * (bar_w - filled), fg=phase_color(seg.phase))
        idx = sorted_segs.index(seg) if seg in sorted_segs else None
        tok = phase_tokens(tl, seg)
        share = tok / tok_total * 100 if tok_total else 0.0
        line = "   " + " ".join([
            f"{rank}.".ljust(3),
            col(f"{seg.node} {seg.phase}".ljust(18), bold=True),
            col(fmt_dur(seg.duration).rjust(8), bold=True),
            f"{pct:5.1f}%".rjust(6),
            bar,
            tok_col(tok, share),
            cache_col(phase_cached(tl, seg), phase_prompt(tl, seg)),
        ])
        out.append((line, idx))

    if tok_total:
        top_tokens = sorted(
            ((s, phase_tokens(tl, s)) for s in tl.segments), key=lambda x: x[1], reverse=True
        )[:8]
        out.append((col("  top token consumers (by phase)", bold=True, fg="gray"), None))
        for rank, (seg, tok) in enumerate(top_tokens, 1):
            if tok <= 0:
                continue
            pct = tok / tok_total * 100
            filled = round(bar_w * min(pct, 100) / 100)
            bar = col("█" * filled + "░" * (bar_w - filled), fg=phase_color(seg.phase))
            idx = sorted_segs.index(seg) if seg in sorted_segs else None
            line = "   " + " ".join([
                f"{rank}.".ljust(3),
                col(f"{seg.node} {seg.phase}".ljust(18), bold=True),
                tok_col(tok),
                f"{pct:5.1f}%".rjust(6),
                bar,
                col(fmt_dur(seg.duration).rjust(8)),
                cache_col(phase_cached(tl, seg), phase_prompt(tl, seg)),
            ])
            out.append((line, idx))

    agents = agent_totals(tl)[:6]
    if agents:
        out.append((col("  by agent (time · tokens · cache)", bold=True, fg="gray"), None))
        for a in agents:
            nodes = ", ".join(a["nodes"])
            share = a["tokens"] / tok_total * 100 if tok_total else 0.0
            line = "   " + " ".join([
                col(a["agent"].ljust(22), bold=True),
                col(fmt_dur(a["total"]).rjust(8), bold=True),
                f"{a['count']} call(s)".rjust(10),
                tok_col(a["tokens"], share),
                cache_col(a["cached"], a["prompt"]),
                nodes,
            ])
            out.append((line, None))
    if tl.visual_steps:
        vtot = sum(v.duration for v in tl.visual_steps)
        line = "   " + " ".join([
            "visual analysis".ljust(22),
            col(fmt_dur(vtot).rjust(8), bold=True),
            "".rjust(10),
            "".rjust(11),
            "",
            f"{len(tl.visual_steps)} image(s)",
        ])
        out.append((line, None))
    gaps = idle_gaps(tl)
    if gaps:
        out.append((col("  longest idle gaps (overhead)", bold=True, fg="gray"), None))
        for gap, label in gaps:
            line = "   " + " ".join([
                "".ljust(3),
                "".ljust(18),
                col(fmt_dur(gap).rjust(8), bold=True),
                "".rjust(6),
                "",
                "",
                label,
            ])
            out.append((line, None))
    if not tok_total:
        out.append((col(
            "  (no token usage recorded — token capture is written by newer ARC builds; "
            "rerun to see tokens here)",
            fg="gray",
        ), None))
    return out


def render_timeline_screen(tl: Timeline, ui: "UIState", width: int, height: int) -> list[str]:
    out = list(header_lines(tl, ui.paused))
    body: list[tuple[str, int | None]] = list(render_gantt(tl, width))
    body.append(("", None))
    body.extend(render_bottlenecks(tl, width))
    flat: list[str] = []
    selectable: list[tuple[int, int]] = []
    for bi, (line, seg_idx) in enumerate(body):
        # Drop the original indent; the selector column (">"/blank) is added
        # below so every row keeps the same fixed-width marker column.
        flat.append(line[2:] if line.startswith("  ") else line)
        if seg_idx is not None:
            selectable.append((bi, seg_idx))

    body_h = max(1, height - len(out) - 1)
    nsel = len(selectable)
    sel = ui.sel % nsel if nsel else 0
    sel_row = selectable[sel][0] if selectable else -1
    if sel_row >= 0:
        if ui.scroll > sel_row:
            ui.scroll = sel_row
        elif sel_row >= ui.scroll + body_h:
            ui.scroll = sel_row - body_h + 1
    ui.scroll = min(ui.scroll, max(0, len(flat) - body_h))
    for i in range(ui.scroll, min(ui.scroll + body_h, len(flat))):
        marker = col(">", fg="cyan", bold=True) if i == sel_row else " "
        row = f"  {marker} {flat[i]}"
        out.append(col(row, rev=True) if i == sel_row else row)
    while len(out) < height:
        out.append("")
    if nsel:
        seg = ordered_segments(tl)[selectable[sel][1]]
        out[height - 1] = col(
            f"  {sel + 1}/{nsel} · Enter: drill into {seg.node} {seg.phase} · 1/2 screens · p pause · q quit",
            fg="gray",
        )
    return out


# ---------------------------------------------------------------------------
# Rendering — steps screen
# ---------------------------------------------------------------------------

def event_desc(e: dict) -> str:
    t = e.get("type")
    if t == "requirement_state":
        return f"{e.get('node_id')} {e.get('phase')} → {e.get('status')}"
    if t == "interface_upsert":
        return f"interface {e.get('interface_id')} upserted"
    if t == "interface_status":
        return f"interface {e.get('interface_id')} → {e.get('status', '')}"
    if t == "test_upsert":
        return f"test {e.get('test_id')} ({e.get('test_type')}) upserted"
    if t == "signal":
        return f"signal: {e.get('reason')}"
    if t == "runner_state":
        return f"runner → {e.get('state')}"
    return str(t)


def replay(tl: Timeline, upto: int) -> dict:
    """Tallies and latest state after replaying steps 0..upto."""
    ifaces: list[str] = []
    tests: list[str] = []
    commits = 0
    runner: str | None = None
    node_phase: dict[str, tuple[str, str]] = {}
    for ev in tl.steps[: upto + 1]:
        d = ev.data
        t = d.get("type")
        if t == "interface_upsert" and d.get("interface_id") not in ifaces:
            ifaces.append(d["interface_id"])
        elif t == "test_upsert" and d.get("test_id") not in tests:
            tests.append(d["test_id"])
        elif t == "signal" and d.get("reason") == "git_commit":
            commits += 1
        elif t == "runner_state":
            runner = d.get("state")
        elif t == "requirement_state":
            node_phase[d.get("node_id")] = (str(d.get("phase")), str(d.get("status")))
    return {
        "ifaces": ifaces,
        "tests": tests,
        "commits": commits,
        "runner": runner,
        "node_phase": node_phase,
    }


def render_steps_screen(tl: Timeline, ui: "UIState", width: int, height: int) -> list[str]:
    n = len(tl.steps)
    ui.step = max(0, min(ui.step, n - 1 if n else 0))
    head = list(header_lines(tl, ui.paused))
    step = ui.step if n else 0
    elapsed = tl.steps[step].epoch - tl.t0 if n else 0.0
    playing_tag = col(" PLAY ", fg="green", bold=True) if ui.playing else ""
    head.insert(
        1,
        col(f"  steps: {step + 1}/{n} · +{fmt_dur(elapsed)} since start {playing_tag}", fg="gray"),
    )

    list_h = max(3, (height - len(head) - 12) // 2)
    window_start = max(0, min(step - list_h // 2, max(0, n - list_h)))
    rows: list[str] = []
    for i in range(window_start, min(window_start + list_h, n)):
        ev = tl.steps[i]
        marker = col(">", fg="cyan", bold=True) if i == step else " "
        line = (
            f"  {marker} [{i}] {col(ev.ts, fg='gray')} "
            f"{col(str(ev.data.get('type')).ljust(18), fg='blue')} {event_desc(ev.data)}"
        )
        rows.append(col(line, rev=True) if i == step else line)

    tally = replay(tl, step) if n else {}
    body: list[str] = rows
    body.append(col("  ── at this step ──", fg="gray"))
    if n:
        body.append("  " + event_desc(tl.steps[step].data))
        body.append(
            f"  tally: {len(tally['ifaces'])} ifaces · {len(tally['tests'])} tests · "
            f"{tally['commits']} commits · runner {tally['runner'] or '—'}"
        )
        cur = tally["node_phase"]
        if cur:
            body.append("  " + " · ".join(f"{k}: {v[0]} {v[1]}" for k, v in list(cur.items())[:5]))
    body.append(col("  ── full event JSON (untruncated) ──", fg="gray"))
    if n:
        pretty = json.dumps(tl.steps[step].data, indent=2, ensure_ascii=False)
        body.extend("  " + w for w in wrap_text(pretty, max(30, width - 2)))

    out = list(head)
    body_h = max(1, height - len(out) - 1)
    ui.scroll = min(ui.scroll, max(0, len(body) - body_h))
    out.extend(body[ui.scroll: ui.scroll + body_h])
    while len(out) < height:
        out.append("")
    out[height - 1] = col(
        "  ←/→ step · space play · g/G ends · 1/2 screens · Esc · q quit", fg="gray"
    )
    return out


# ---------------------------------------------------------------------------
# Rendering — drill-down (phase internals) and call detail
# ---------------------------------------------------------------------------

_LAYER_COLORS = {"Integration": "cyan", "E2E": "magenta", "Unit": "green"}


def layer_color(layer: str) -> str:
    if layer in _LAYER_COLORS:
        return _LAYER_COLORS[layer]
    if layer:
        return "yellow"
    return "gray"


def render_drill_screen(tl: Timeline, ui: "UIState", width: int, height: int) -> list[str]:
    seg = ui.drill_seg
    calls = phase_calls(tl, seg.node, seg.phase)
    out = [
        col(f" drill: {seg.node} {seg.phase}", bold=True, fg="blue")
        + col(f"  {fmt_dur(seg.duration)}", bold=True)
        + f"  · {len(calls)} agent call(s) · status {seg.status}",
        col(f"  {fmt_ts(seg.start)} → {fmt_ts(seg.end)}   ▲▼ select · Enter: call detail · Esc back · q quit", fg="gray"),
    ]
    if not calls:
        out.append(col("  (no agent calls recorded for this phase)", fg="gray"))
        while len(out) < height:
            out.append("")
        return out

    sel = ui.sel % len(calls)
    span = max(seg.duration, 1e-6)
    # Fixed-width tail keeps tokens/cache/cost columns aligned across rows:
    # prefix(12) + duration(8+2) + bar gap(2) + agent(32) + tokens(12) + cache(13) + cost(9)
    tail_w = 90
    bar_w = max(10, min(width - tail_w, 80))
    body: list[str] = []
    body.append(col(
        f"  ── {len(calls)} calls, grouped by layer · attempt = one fix-test loop ──",
        fg="gray",
    ))
    sel_body_row: int | None = None
    flat_of = {id(c): i for i, c in enumerate(calls)}
    for g in group_calls(tl, seg.node, seg.phase):
        gcolor = layer_color(g["layer"])
        g_calls_n = len(g["calls"])
        g_total = fmt_dur(g["total"])
        g_tools = fmt_dur(g["tools_total"])
        g_thinking = fmt_dur(g["thinking_total"])
        g_tokens = g["tokens"]
        tokens_str = f" · tokens {fmt_tokens(g_tokens)}" if g_tokens else ""
        cache_str = ""
        if g["prompt"]:
            cache_str = f" · cache {g['cached'] / g['prompt'] * 100:4.1f}%"
        g_cost = sum(c.cost for c in g["calls"] if c.cost is not None)
        run_cost = total_cost(tl)
        cost_str = ""
        if g_cost:
            share = g_cost / run_cost * 100 if run_cost else 0.0
            cost_str = f" · cost {fmt_cost(g_cost)} ({share:.1f}%)"
        body.append(
            f"  {col(g['layer'], fg=gcolor, bold=True)}  "
            f"{col(f'{g_calls_n} call(s) · {g_total}', bold=True)}  "
            f"(tools {g_tools} · model {col(g_thinking, fg='yellow')}){tokens_str}{cache_str}{cost_str}"
        )
        for ci, call in enumerate(g["calls"]):
            flat = flat_of[id(call)]
            c0 = max(0, min(int((call.start_epoch - seg.start) / span * bar_w), bar_w - 1))
            c1 = min(max(c0, int((call.end_epoch - seg.start) / span * bar_w)), bar_w - 1)
            cells = ["░"] * bar_w
            for bi in range(c0, c1 + 1):
                cells[bi] = "█"
            bar = col("".join(cells), fg=gcolor, bold=True)
            # Single-width ASCII marker: a double-width glyph (e.g. "▶") would
            # shift the bar column when the selection moves.
            marker = col(">", fg="cyan", bold=True) if flat == sel else " "
            attempt = f"[{ci + 1}/{g_calls_n}]".ljust(7)
            tokens_str = f"  {fmt_tokens(call.total_tokens).rjust(10)}" if call.has_usage else ""
            cache_str = f"  cache {call.cache_rate:4.1f}%" if call.prompt_tokens else ""
            cost_str = f"  {fmt_cost(call.cost).rjust(7)}" if call.cost is not None else ""
            agent = f"{call.agent} {call.layer}".strip()
            # NOTE: adjacent f-strings implicitly concatenate BEFORE a trailing
            # .strip() binds — which would strip the leading indent. Use explicit
            # "+" so .strip() applies only to the agent label.
            line = (
                f"  {marker} {attempt} "
                + f"{col(fmt_dur(call.duration).rjust(8), bold=True)}  {bar}  "
                + col(agent[:32].ljust(32)) + tokens_str + cache_str + cost_str
            )
            if flat == sel:
                line = col(line, rev=True)
                sel_body_row = len(body)
            body.append(line)
        body.append("")

    body.append("")
    body.append(col("  slowest calls in this phase  (> = selected)", bold=True, fg="gray"))
    selected_id = calls[sel]
    for rank, call in enumerate(sorted(calls, key=lambda c: c.duration, reverse=True)[:8], 1):
        pct = call.duration / seg.duration * 100 if seg.duration > 0 else 0.0
        exec_total, thinking, tools = call_breakdown(tl, call)
        marker = "> " if call is selected_id else "  "
        plain = f"{call.agent} {call.layer}".strip()
        body.append(
            f"   {marker}{rank:>2}. {col(plain[:40].ljust(40), bold=True)} "
            f"{col(fmt_dur(call.duration).rjust(9), bold=True)}  {pct:4.1f}%  "
            f"tools {fmt_dur(exec_total)} · model {col(fmt_dur(thinking), fg='yellow')}"
        )
    gaps = phase_gaps(tl, seg.node, seg.phase)
    if gaps:
        body.append(col("  idle gaps inside phase", bold=True, fg="gray"))
        for gap, label in gaps:
            body.append(f"   {col(fmt_dur(gap), bold=True):>9}  {label}")
    if not any(c.has_usage for c in calls):
        body.append(col(
            "  (no token usage recorded for this run — token capture is written to debug.log "
            "by newer ARC builds; rerun to see tokens here)",
            fg="gray",
        ))

    body_h = max(1, height - len(out) - 1)
    if sel_body_row is not None:
        if ui.scroll > sel_body_row:
            ui.scroll = sel_body_row
        elif sel_body_row >= ui.scroll + body_h:
            ui.scroll = sel_body_row - body_h + 1
    ui.scroll = min(ui.scroll, max(0, len(body) - body_h))
    out.extend(body[ui.scroll: ui.scroll + body_h])
    while len(out) < height:
        out.append("")
    sel_call = calls[sel]
    out[height - 1] = col(
        f"  {sel + 1}/{len(calls)} · ▲▼ select: {sel_call.agent} {sel_call.layer}".strip()
        + f"  ({fmt_dur(sel_call.duration)}) · Enter: inspect · Esc back · q quit",
        fg="gray",
    )
    return out


def render_call_screen(tl: Timeline, ui: "UIState", width: int, height: int) -> list[str]:
    calls = phase_calls(tl, ui.drill_seg.node, ui.drill_seg.phase)
    if not calls:
        return render_drill_screen(tl, ui, width, height)
    call = calls[ui.call_sel % len(calls)]
    exec_total, thinking, tools = call_breakdown(tl, call)
    if call.has_usage:
        usage_str = (
            f"tokens {fmt_tokens(call.total_tokens)}"
            f" (prompt {fmt_tokens(call.prompt_tokens)} · completion {fmt_tokens(call.completion_tokens)}"
            f" · cache {call.cache_rate:.1f}% · {call.llm_calls} LLM call(s))"
        )
    else:
        usage_str = "tokens — (no usage recorded in this run's log)"
    cost = call.cost
    if cost is not None:
        run_cost = total_cost(tl)
        cum = cumulative_cost(tl, call)
        share = cost / run_cost * 100 if run_cost else 0.0
        cum_share = cum / run_cost * 100 if run_cost else 0.0
        cost_str = (
            f"cost {fmt_cost(cost)} · cumulative {fmt_cost(cum)} ({cum_share:.1f}% of run"
            f"{' · this call ' + f'{share:.1f}%' if share else ''}"
            f"{', model ' + call.model if call.model else ''})"
        )
    elif call.has_usage:
        cost_str = (
            f"cost — (no price for model {call.model or '?'}; set "
            "ARC_INPUT_PRICE_PER_1M / ARC_OUTPUT_PRICE_PER_1M to enable)"
        )
    else:
        cost_str = ""
    out = [
        col(f" call: {call.agent} {call.layer}".strip(), bold=True, fg="blue")
        + col(f"  {fmt_dur(call.duration)}", bold=True)
        + f"  · {call.node} {call.phase}"
        + f"  · {fmt_ts(call.start_epoch)} → {fmt_ts(call.end_epoch)}",
        col(
            f"  tool execution {fmt_dur(exec_total)} · model thinking {fmt_dur(thinking)} "
            f"({round(thinking / max(call.duration, 1e-6) * 100)}%) · "
            f"{len(tools)} tool call(s) · {usage_str} · Esc back · q quit",
            fg="gray",
        ),
    ]
    if cost_str:
        out.insert(1, col("  " + cost_str, fg="gray"))
    if not tools:
        out.append(col("  (no tool calls recorded)", fg="gray"))
        while len(out) < height:
            out.append("")
        return out

    total = max(call.duration, 1e-6)
    body: list[str] = []
    for t in tools:
        pct = t.duration / total * 100
        bar_w = max(6, min(24, width // 4))
        filled = round(bar_w * min(pct, 100) / 100)
        bar = col("█" * filled + "░" * (bar_w - filled), fg="yellow" if t.kind == "tool-call" else "gray")
        name = t.name or t.kind
        body.append(
            f"  {col(fmt_dur(t.duration).rjust(9), bold=True)}  {pct:5.1f}%  {bar}  {name}"
            + (f"  {t.detail[: max(20, width - 60)]}" if t.detail else "")
        )
    body.append("")
    body.append(col("  slowest tool calls", bold=True, fg="gray"))
    for rank, t in enumerate(sorted(tools, key=lambda x: x.duration, reverse=True)[:6], 1):
        body.append(f"   {rank}. {col(fmt_dur(t.duration), bold=True):>9}  {t.name or t.kind}")

    body.append("")
    body.append(col("  ── full prompt received by this call ──", bold=True, fg="gray"))
    if not call.prompt_text and not call.model_inputs:
        body.append(col(
            "  (not logged — rerun with ARC_DEBUG_AGENT_TRACE=1 to capture the "
            "full prompt at every agent call)",
            fg="gray",
        ))
    if call.prompt_text:
        header = call.prompt_text.split("\n", 1)[0]
        body.append(col(f"  {header}", fg="gray"))
        rest = call.prompt_text.split("\n", 1)[1] if "\n" in call.prompt_text else ""
        for w in wrap_text(rest, max(30, width - 4)):
            body.append("  " + w)
        body.append("")
    for idx, block in enumerate(call.model_inputs, 1):
        body.append(col(
            f"  ── model input #{idx} (system + human messages sent to the model) ──",
            bold=True,
            fg="cyan",
        ))
        for w in wrap_text(block, max(30, width - 4)):
            body.append("  " + w)
        body.append("")

    body_h = max(1, height - len(out) - 1)
    ui.scroll = min(ui.scroll, max(0, len(body) - body_h))
    out.extend(body[ui.scroll: ui.scroll + body_h])
    while len(out) < height:
        out.append("")
    top = ui.scroll + 1
    bottom = min(ui.scroll + body_h, len(body))
    out[height - 1] = col(
        f"  call {ui.call_sel + 1}/{len(calls)} · {top}-{bottom} of {len(body)} rows · "
        "↑↓/j k scroll · ←/→ next call · Esc back · q quit",
        fg="gray",
    )
    return out


# ---------------------------------------------------------------------------
# TUI loop
# ---------------------------------------------------------------------------

@dataclass
class UIState:
    """Mutable TUI state shared between the input loop and the renderers."""

    screen: str = "timeline"       # timeline | steps | drill | call
    step: int = 0
    playing: bool = False
    paused: bool = False
    scroll: int = 0
    sel: int = 0                   # selection on timeline screen (segments)
    last_play: float = 0.0
    play_interval: float = 0.35
    drill_seg: Segment | None = None   # phase being drilled into
    call_sel: int = 0                  # selected agent call inside the phase


def snapshot(tl: Timeline) -> str:
    width, _ = terminal_size()
    tokens = total_tokens(tl)
    tokens_str = f"  ·  {fmt_tokens(tokens)} tokens" if tokens else ""
    out = [col("ARC timeline — snapshot", bold=True, fg="blue") + tokens_str]
    out.extend(header_lines(tl, False)[1:])
    out.extend(line for line, _ in render_gantt(tl, width))
    out.append("")
    out.extend(line for line, _ in render_bottlenecks(tl, width))
    return "\n".join(out)


def run_tui(tl_factory) -> int:
    from core.termui import Input, fit_frame

    inp = Input()
    inp.enter()
    atexit.register(inp.leave)
    resize = {"flag": False}

    def on_winch(signum, frame):  # noqa: ARG001
        resize["flag"] = True

    signal.signal(signal.SIGWINCH, on_winch)

    ui = UIState()
    tl = tl_factory()
    last_fetch = 0.0
    dirty = True
    first = True
    last_len = len(tl.steps)

    hide_cursor()
    atexit.register(show_cursor)
    try:
        while True:
            # --- input ---
            while True:
                key = inp.read_key(0.0)
                if key is None:
                    break
                if key == "q":
                    return 0
                if key == "p":
                    ui.paused = not ui.paused
                    dirty = True
                    continue
                if key in ("1", "2"):
                    ui.screen = "timeline" if key == "1" else "steps"
                    ui.scroll = 0
                    ui.drill_seg = None
                    dirty = True
                    continue
                if ui.screen in ("drill", "call"):
                    seg = ui.drill_seg
                    if seg is None:
                        ui.screen = "timeline"
                        dirty = True
                        continue
                    if ui.screen == "drill":
                        calls = phase_calls(tl, seg.node, seg.phase)
                        nsel = len(calls)
                        if key in ("down", "j"):
                            ui.sel = (ui.sel + 1) % max(nsel, 1)
                            dirty = True
                        elif key in ("up", "k"):
                            ui.sel = (ui.sel - 1) % max(nsel, 1)
                            dirty = True
                        elif key == "enter" and nsel:
                            ui.call_sel = ui.sel % nsel
                            ui.screen = "call"
                            ui.scroll = 0
                            dirty = True
                        elif key == "esc":
                            ui.screen = "timeline"
                            ui.scroll = 0
                            ui.drill_seg = None
                            dirty = True
                    else:  # call — scroll the tool list; Tab switches calls
                        calls = phase_calls(tl, seg.node, seg.phase)
                        nsel = len(calls)
                        if key in ("down", "j"):
                            ui.scroll += 1
                            dirty = True
                        elif key in ("up", "k"):
                            ui.scroll = max(0, ui.scroll - 1)
                            dirty = True
                        elif key == "pgup":
                            ui.scroll = max(0, ui.scroll - 20)
                            dirty = True
                        elif key == "pgdn":
                            ui.scroll += 20
                            dirty = True
                        elif key in ("home", "g"):
                            ui.scroll = 0
                            dirty = True
                        elif key in ("end", "G"):
                            ui.scroll = 1 << 30  # clamped to the bottom by the renderer
                            dirty = True
                        elif key in ("left", "h"):
                            if nsel:
                                ui.call_sel = (ui.call_sel - 1) % nsel
                            ui.scroll = 0
                            dirty = True
                        elif key in ("right", "l"):
                            if nsel:
                                ui.call_sel = (ui.call_sel + 1) % nsel
                            ui.scroll = 0
                            dirty = True
                        elif key == "esc":
                            ui.screen = "drill"
                            ui.sel = ui.call_sel
                            ui.scroll = 0
                            dirty = True
                    continue
                if ui.screen == "timeline":
                    segs = ordered_segments(tl)
                    nseg = len(segs)
                    if key in ("down", "j", "tab"):
                        if nseg:
                            ui.sel = (ui.sel + 1) % nseg
                        dirty = True
                    elif key in ("up", "k", "shift-tab"):
                        if nseg:
                            ui.sel = (ui.sel - 1) % nseg
                        dirty = True
                    elif key == "enter" and nseg:
                        ui.drill_seg = segs[ui.sel % nseg]
                        ui.screen = "drill"
                        ui.scroll = 0
                        ui.sel = 0
                        dirty = True
                    continue
                if ui.screen == "steps":
                    n = len(tl.steps)
                    if key in ("left", "h"):
                        ui.step = max(0, ui.step - 1)
                        ui.playing = False
                        dirty = True
                    elif key in ("right", "l"):
                        if ui.step < n - 1:
                            ui.step += 1
                        dirty = True
                    elif key in ("home", "g"):
                        ui.step = 0
                        ui.playing = False
                        dirty = True
                    elif key in ("end", "G"):
                        ui.step = max(0, n - 1)
                        ui.playing = False
                        dirty = True
                    elif key == "esc":
                        ui.screen = "timeline"
                        ui.scroll = 0
                        dirty = True
                if key == " " and ui.screen == "steps":
                    ui.playing = not ui.playing
                    ui.last_play = time.monotonic()
                    if ui.step >= len(tl.steps) - 1:
                        ui.step = 0
                    dirty = True

            # --- autoplay stepping ---
            if ui.playing and not ui.paused:
                now = time.monotonic()
                if now - ui.last_play >= ui.play_interval:
                    ui.last_play = now
                    if ui.step < len(tl.steps) - 1:
                        ui.step += 1
                        dirty = True
                    else:
                        ui.playing = False
                        dirty = True

            # --- refresh ---
            now = time.monotonic()
            if not ui.paused and now - last_fetch >= 1.0:
                last_fetch = now
                new_tl = tl_factory()
                changed = len(new_tl.steps) != last_len
                if changed:
                    last_len = len(new_tl.steps)
                    tl = new_tl
                    dirty = True
                elif new_tl.runner_state != tl.runner_state:
                    tl = new_tl
                    dirty = True

            # --- render ---
            if dirty or resize["flag"] or (tl.runner_state == "running" and not ui.paused):
                resize["flag"] = False
                dirty = False
                width, height = terminal_size()
                if ui.screen == "steps":
                    frame = render_steps_screen(tl, ui, width, height)
                elif ui.screen == "drill" and ui.drill_seg is not None:
                    frame = render_drill_screen(tl, ui, width, height)
                elif ui.screen == "call" and ui.drill_seg is not None:
                    frame = render_call_screen(tl, ui, width, height)
                else:
                    frame = render_timeline_screen(tl, ui, width, height)
                out = fit_frame(frame, width, height)
                if first:
                    first = False
                    out = "\x1b[2J" + out
                sys.stdout.write(out)
                sys.stdout.flush()
            else:
                inp.pending(min(0.05, 1.0 - (time.monotonic() - last_fetch)))
    except KeyboardInterrupt:
        return 0
    finally:
        show_cursor()
        inp.leave()
        sys.stdout.write("\n")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None, *, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=prog or ("arc-timeline.py" if argv is None else "arc timeline"),
        description="Step-through timeline and bottleneck analysis of an ARC compilation.",
    )
    parser.add_argument("output_dir", help="ARC output workspace directory (contains .arc/)")
    args = parser.parse_args(argv)

    if os.environ.get("NO_COLOR"):
        set_color(False)

    workspace = Path(args.output_dir).expanduser().resolve()
    if not workspace.is_dir():
        print(f"error: {workspace} is not a directory")
        return 1
    if not (workspace / ".arc").is_dir():
        print(f"error: {workspace} is not an ARC workspace (missing .arc/ dir)")
        return 1

    if not sys.stdout.isatty():
        print(snapshot(load_timeline(workspace)))
        return 0

    print(f"analyzing {workspace}", flush=True)
    try:
        return run_tui(lambda: load_timeline(workspace))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
