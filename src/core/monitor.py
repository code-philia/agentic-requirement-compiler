#!/usr/bin/env python3
"""core.monitor — terminal progress monitor for an ARC compilation workspace.

Run it as an ARC subcommand:
    arc monitor <output-dir>

It polls the workspace's .arc artifacts (processing_queue.json, runner-events.jsonl,
traceability/*, debug.log) and renders a live terminal dashboard with two screens:

  [1] Overview  — runner state, queue progress, requirement nodes, interfaces,
                  tests, recent events, full debug.log tail (scrollable)
  [2] Graphs    — requirement tree and interface call graph (terminal canvas)

Keybindings:
  1 / 2           switch screens (overview / graphs)
  3 / 4           within graphs: requirement tree / interface call graph
  j k / arrows    move selection / scroll
  Enter           open full detail (untruncated, scrollable)
  Esc             back / close detail
  p               pause / resume live polling
  f               toggle follow (stick overview scroll to the newest log lines)
  q               quit

If stdout is not a terminal it prints one static snapshot instead.
"""

from __future__ import annotations

import argparse
import atexit
import json
import math
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.termui import (
    Input,
    Row,
    col,
    hide_cursor,
    set_color,
    show_cursor,
    state_color,
    terminal_size,
    type_color,
    wrap_text,
)

# ---------------------------------------------------------------------------
# File readers (robust against in-progress writes)
# ---------------------------------------------------------------------------

def read_json(path: Path, default):
    for attempt in range(2):
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except json.JSONDecodeError:
            if attempt == 0:
                time.sleep(0.05)
                continue
            return default
        except OSError:
            return default
    return default


def tail_lines(path: Path, n: int, chunk: int = 1 << 16) -> list[str]:
    """Return the last `n` lines without reading the whole file."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size <= 0:
        return []
    data = b""
    read_from = max(0, size - chunk)
    with path.open("rb") as fh:
        fh.seek(read_from)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    # If the first chunk didn't reach line boundaries, pull an earlier chunk.
    if len(lines) <= n and read_from > 0:
        read_from2 = max(0, read_from - chunk)
        with path.open("rb") as fh:
            fh.seek(read_from2)
            data2 = fh.read(chunk)
        lines = (data2.decode("utf-8", errors="replace").splitlines() + lines)[- (n + 1):]
    return lines[-n:]


def read_jsonl_all(path: Path) -> list[str]:
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.strip()]


def read_events(path: Path) -> tuple[dict | None, list[dict]]:
    """Parse the runner event stream (append-only, small by design — it only
    records state transitions and artifact upserts, not agent tool calls).

    Returns (runner_state, compact_events) with absolute line indices so a
    detail view can fetch the exact raw line.
    """
    runner: dict | None = None
    events: list[dict] = []
    for idx, raw in enumerate(read_jsonl_all(path)):
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "runner_state":
            runner = {
                "state": ev.get("state"),
                "message": ev.get("message"),
                "timestamp": ev.get("timestamp"),
            }
        elif etype == "requirement_state":
            events.append(
                {
                    "line": idx, "type": etype,
                    "node_id": ev.get("node_id"), "phase": ev.get("phase"),
                    "status": ev.get("status"), "timestamp": ev.get("timestamp"),
                }
            )
        elif etype in ("interface_upsert", "interface_status", "test_upsert"):
            events.append(
                {
                    "line": idx, "type": etype,
                    "interface_id": ev.get("interface_id"), "status": ev.get("status"),
                    "test_id": ev.get("test_id"), "test_type": ev.get("test_type"),
                    "timestamp": ev.get("timestamp"),
                }
            )
        elif etype == "signal":
            events.append(
                {"line": idx, "type": etype, "reason": ev.get("reason"), "timestamp": ev.get("timestamp")}
            )
    return runner, events[-60:]


# ---------------------------------------------------------------------------
# State aggregation
# ---------------------------------------------------------------------------

class Monitor:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.arc = workspace / ".arc"

    def state(self) -> dict:
        arc = self.arc
        runner, events = read_events(arc / "runner-events.jsonl")

        queue = read_json(arc / "processing_queue.json", {})
        node_states = read_json(arc / "traceability" / "node_states.json", {})
        requirements = read_json(arc / "traceability" / "requirements.json", {})
        interfaces_raw = read_json(arc / "traceability" / "interfaces.json", {})
        tests_raw = read_json(arc / "traceability" / "tests.json", {})
        call_edges_raw = read_json(arc / "traceability" / "call_edges.json", {})

        node_ids = set(node_states) | set(requirements) | set(
            t.get("node_id") for t in queue.get("tasks", []) if t.get("node_id")
        )
        # Execution order: the order nodes entered the pipeline, taken from the
        # queue task order (ROOT:DESIGN is always first, so the list starts at ROOT).
        exec_order: dict[str, int] = {}
        for task in sorted(queue.get("tasks", []), key=lambda t: t.get("order", 0)):
            nid = task.get("node_id")
            if nid and nid not in exec_order:
                exec_order[nid] = len(exec_order)
        far = 10**9
        nodes: list[dict] = []
        for nid in sorted(node_ids, key=lambda n: (exec_order.get(n, far), n.count("."), n)):
            entry = requirements.get(nid, {})
            st = node_states.get(nid, {})
            session_path = arc / "node_sessions" / f"{nid}.json"
            nodes.append(
                {
                    "id": nid,
                    "order": exec_order.get(nid, far),
                    "name": entry.get("name") or nid,
                    "state": st.get("state"),
                    "phase": st.get("phase"),
                    "updated_at": st.get("updated_at"),
                    "parent": entry.get("parent_id") or (None if nid == "ROOT" else "ROOT"),
                    "children": entry.get("children_ids", []),
                    "has_session": session_path.is_file(),
                }
            )

        interfaces = []
        for iid, rec in interfaces_raw.items():
            interfaces.append(
                {
                    "interface_id": iid,
                    "req_ids": rec.get("req_ids", []),
                    "type": rec.get("type"),
                    "file_path": rec.get("file_path"),
                    "first_line": rec.get("first_line"),
                    "implemented": bool(rec.get("implemented")),
                    "callers": rec.get("callers", []),
                    "callees": rec.get("callees", []),
                }
            )
        interfaces.sort(key=lambda i: (i.get("file_path") or "", i["interface_id"]))

        tests = []
        for tid, rec in tests_raw.items():
            tests.append(
                {
                    "test_id": tid,
                    "req_id": rec.get("req_id"),
                    "type": rec.get("type"),
                    "file_path": rec.get("file_path"),
                    "passed": rec.get("passed"),
                    "first_line": rec.get("first_line"),
                }
            )
        tests.sort(key=lambda t: (t.get("type") or "", t["test_id"]))

        call_edges = []
        for rec in call_edges_raw.values():
            if rec.get("from_interface_id") and rec.get("to_interface_id"):
                call_edges.append(
                    {
                        "from": rec["from_interface_id"],
                        "to": rec["to_interface_id"],
                        "edge_type": rec.get("edge_type"),
                        "source_req_id": rec.get("source_req_id"),
                        "target_req_id": rec.get("target_req_id"),
                    }
                )

        return {
            "workspace": str(self.workspace),
            "server_time": time.strftime("%H:%M:%S"),
            "runner": runner,
            "queue": {"tasks": sorted(queue.get("tasks", []), key=lambda t: t.get("order", 0))},
            "nodes": nodes,
            "interfaces": interfaces,
            "tests": tests,
            "call_edges": call_edges,
            "events": events,
            "log": tail_lines(arc / "debug.log", 40),
        }

    def detail(self, kind: str, item_id: str) -> dict:
        arc = self.arc
        if kind == "node":
            entry = read_json(arc / "traceability" / "requirements.json", {}).get(item_id, {})
            st = read_json(arc / "traceability" / "node_states.json", {}).get(item_id, {})
            session = read_json(arc / "node_sessions" / f"{item_id}.json", {})
            return {
                "kind": "node", "id": item_id, "entry": entry, "state": st,
                "session_summary": {
                    "phase_status": session.get("phase_status"),
                    "recent_failure_summary": session.get("recent_failure_summary", ""),
                },
            }
        if kind == "interface":
            rec = read_json(arc / "traceability" / "interfaces.json", {}).get(item_id, {})
            content = rec.get("content")
            try:
                content_parsed = json.loads(content) if isinstance(content, str) else {}
            except json.JSONDecodeError:
                content_parsed = {}
            return {"kind": "interface", "id": item_id, "record": rec, "content_parsed": content_parsed}
        if kind == "test":
            rec = read_json(arc / "traceability" / "tests.json", {}).get(item_id, {})
            return {"kind": "test", "id": item_id, "record": rec}
        if kind == "session":
            path = arc / "node_sessions" / f"{item_id}.json"
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            except OSError:
                return {"kind": "session", "id": item_id, "error": "no session file", "pretty": ""}
            except json.JSONDecodeError:
                return {"kind": "session", "id": item_id, "error": "session file unreadable", "pretty": ""}
            return {"kind": "session", "id": item_id, "pretty": pretty}
        if kind == "event":
            raw_lines = read_jsonl_all(arc / "runner-events.jsonl")
            try:
                line = int(item_id)
                raw = raw_lines[line]
                parsed = json.loads(raw)
            except (ValueError, IndexError, json.JSONDecodeError):
                return {"kind": "event", "id": item_id, "error": "event line not found", "raw": "", "pretty": ""}
            return {
                "kind": "event", "id": item_id, "raw": raw,
                "pretty": json.dumps(parsed, indent=2, ensure_ascii=False),
            }
        return {"kind": kind, "id": item_id, "error": f"unknown detail kind: {kind}", "raw": "", "pretty": ""}


# ---------------------------------------------------------------------------
# Rendering — overview
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


def build_overview(state: dict, width: int) -> list[Row]:
    rows: list[Row] = []
    tasks = state["queue"]["tasks"]
    done = sum(1 for t in tasks if t.get("status") == "COMPLETED")
    running = sum(1 for t in tasks if t.get("status") == "RUNNING")
    pct = round(100 * done / len(tasks)) if tasks else 0
    bar_w = max(8, min(width - 30, 40))
    filled = round(bar_w * done / len(tasks)) if tasks else 0
    bar = col("█" * filled + "░" * (bar_w - filled), fg="cyan")
    rows.append(Row(f"  {bar}  {done}/{len(tasks)} tasks done · {running} running · {pct}%"))
    rows.append(Row(col("  queue", bold=True, fg="gray")))
    for t in tasks:
        fg, bold = state_color(t.get("status", "PENDING"))
        rows.append(Row(
            f"  {col(str(t.get('status','')).ljust(10), fg=fg, bold=bold)} "
            f"{col(str(t.get('node_id')), bold=True)}:{str(t.get('phase')).lower()}",
            kind="node", id=t.get("node_id"),
        ))
    rows.append(Row(""))
    rows.append(Row(col("  requirement nodes", bold=True, fg="gray")))
    for n in state["nodes"]:
        st = n.get("state") or "UNSEEN"
        fg, bold = state_color(st)
        name = n.get("name") or n["id"]
        rows.append(Row(
            f"  {col(n['id'].ljust(11), bold=True)} {col(st.ljust(13), fg=fg, bold=bold)} "
            f"{name[: max(10, width - 46)]}",
            kind="node", id=n["id"],
        ))
    rows.append(Row(""))
    rows.append(Row(col("  interfaces", bold=True, fg="gray")))
    for i in state["interfaces"]:
        impl = "✓" if i["implemented"] else "·"
        rows.append(Row(
            f"  {col(impl, fg=('green' if i['implemented'] else 'gray'))} "
            f"{col(i['type'].ljust(5), fg=type_color(i['type']))} {col(i['interface_id'], bold=True)} "
            f"{i['file_path']}",
            kind="interface", id=i["interface_id"],
        ))
    rows.append(Row(""))
    rows.append(Row(col("  tests", bold=True, fg="gray")))
    for t in state["tests"]:
        if t["passed"] is True:
            res = col("PASS", fg="green", bold=True)
        elif t["passed"] is False:
            res = col("FAIL", fg="red", bold=True)
        else:
            res = col("····", fg="gray")
        rows.append(Row(
            f"  {res}  {col(t['type'].ljust(5), fg=type_color(t['type']))} {col(t['test_id'], bold=True)} "
            f"{t['file_path']}",
            kind="test", id=t["test_id"],
        ))
    rows.append(Row(""))
    rows.append(Row(col("  recent events  (Enter for full JSON)", bold=True, fg="gray")))
    collapsed: list[dict] = []
    for e in reversed(state["events"]):
        if (
            collapsed
            and e.get("type") == "signal"
            and collapsed[-1].get("type") == "signal"
            and collapsed[-1].get("reason") == e.get("reason")
        ):
            collapsed[-1]["count"] = collapsed[-1].get("count", 1) + 1
        else:
            collapsed.append(dict(e))
    for e in collapsed:
        ts = e.get("timestamp") or ""
        desc = event_desc(e)
        if e.get("count"):
            desc += col(f"  ×{e['count']}", fg="yellow")
        rows.append(Row(
            f"  {col(ts, fg='gray')} {col(str(e.get('type')).ljust(18), fg='blue')} {desc}",
            kind="event", id=str(e["line"]),
        ))
    rows.append(Row(""))
    rows.append(Row(col("  debug.log tail  (full lines, no truncation)", bold=True, fg="gray")))
    for line in state["log"]:
        for wrapped in wrap_text(line, max(20, width - 2)):
            rows.append(Row(f"  {wrapped}"))
    return rows


def header_lines(state: dict, paused: bool, width: int) -> list[str]:
    runner = state["runner"]
    st = (runner.get("state") or "NO RUN").upper() if runner else "NO RUN"
    fg, bold = state_color(st)
    badge = col(f" [{st}] ", fg=fg, bold=True)
    tasks = state["queue"]["tasks"]
    done = sum(1 for t in tasks if t.get("status") == "COMPLETED")
    prog = f" · {done}/{len(tasks)} tasks" if tasks else ""
    counts = (
        f"{len(state['interfaces'])} ifaces · {len(state['tests'])} tests"
        f"{prog} · {state['server_time']}"
    )
    paused_tag = col(" PAUSED ", fg="yellow", bold=True) if paused else ""
    return [
        col(" ARC monitor", bold=True, fg="blue") + badge + counts + paused_tag,
        col("  " + state["workspace"], fg="gray"),
    ]


def render_overview(state: dict, ui: "UIState", width: int, height: int) -> list[str]:
    rows = build_overview(state, width)
    total = len(rows)
    sel_row = ui.sel % total if total else 0
    has_detail = any(r.kind for r in rows)

    head = header_lines(state, ui.paused, width)
    body_h = max(1, height - len(head) - 1)
    # keep selection visible
    if sel_row >= 0:
        if ui.ov_scroll > sel_row:
            ui.ov_scroll = sel_row
        elif sel_row >= ui.ov_scroll + body_h:
            ui.ov_scroll = sel_row - body_h + 1
    if ui.follow and state["log"]:
        ui.ov_scroll = max(0, total - body_h)
    scroll = min(ui.ov_scroll, max(0, total - body_h))
    ui.ov_scroll = scroll

    out = list(head)
    for i in range(scroll, min(scroll + body_h, total)):
        r = rows[i]
        if i == sel_row:
            out.append(col(r.line, rev=True))
        else:
            out.append(r.line)
    # fill remaining with a status line
    while len(out) < height:
        out.append("")
    if total:
        hint = "Enter inspect" if has_detail else "Enter —"
        out[height - 1] = col(
            f"  {sel_row + 1}/{total} · {hint} · 1/2 views · p pause · q quit",
            fg="gray",
        )
    return out


# ---------------------------------------------------------------------------
# Rendering — graphs
# ---------------------------------------------------------------------------

class Graph:
    def __init__(self) -> None:
        self.cache_key = None
        self.positions: dict[str, tuple[float, float]] = {}

    def layout(self, state: dict, mode: str) -> tuple[list[dict], list[tuple[str, str]], dict[str, tuple[float, float]], dict[str, dict]]:
        if mode == "tree":
            return self._tree_layout(state)
        return self._call_layout(state)

    def _tree_layout(self, state: dict) -> tuple[list[dict], list[tuple[str, str]], dict[str, tuple[float, float]], dict[str, dict]]:
        nodes = state["nodes"]
        by_id = {n["id"]: n for n in nodes}
        if "ROOT" not in by_id:
            kids = [n["id"] for n in nodes if (n.get("parent") == "ROOT" or not n.get("parent"))]
            by_id["ROOT"] = {"id": "ROOT", "name": "ROOT", "state": None, "parent": None, "children": kids}
        ids = set(by_id)
        edges: list[tuple[str, str]] = []
        kids_of: dict[str, list[str]] = {}
        for nid in ids:
            n = by_id[nid]
            for c in (n.get("children") or []):
                if c in ids:
                    edges.append((nid, c))
                    kids_of.setdefault(nid, []).append(c)
        return list(by_id.values()), edges, {}, by_id

    def _call_layout(self, state: dict) -> tuple[list[dict], list[tuple[str, str]], dict[str, tuple[float, float]], dict[str, dict]]:
        ifaces = state["interfaces"]
        nodes = [{"id": i["interface_id"], "type": i["type"], "implemented": i["implemented"]} for i in ifaces]
        node_ids = {n["id"] for n in nodes}
        seen: set[tuple[str, str]] = set()
        edges: list[tuple[str, str]] = []
        for e in state["call_edges"]:
            a, b = e["from"], e["to"]
            if a in node_ids and b in node_ids and (a, b) not in seen:
                seen.add((a, b))
                edges.append((a, b))
        by_id = {i["interface_id"]: i for i in ifaces}
        for i in ifaces:
            for c in (i.get("callees") or []):
                if c in node_ids and (i["interface_id"], c) not in seen:
                    seen.add((i["interface_id"], c))
                    edges.append((i["interface_id"], c))
        key = "|".join(sorted(n["id"] for n in nodes)) + "#" + "|".join(sorted(f"{a}>{b}" for a, b in edges))
        if key != self.cache_key:
            self.cache_key = key
            n = len(nodes)
            if n == 1:
                self.positions = {nodes[0]["id"]: (50.0, 50.0)}
            elif n > 1:
                pos = {nd["id"]: (50 + 38 * math.cos(2 * math.pi * i / n), 50 + 38 * math.sin(2 * math.pi * i / n))
                       for i, nd in enumerate(nodes)}
                k, rep, c0, iters = 0.06, 900.0, 300.0, 400
                for _ in range(iters):
                    force = {nd["id"]: (0.0, 0.0) for nd in nodes}
                    for i in range(n):
                        for j in range(i + 1, n):
                            a, b = nodes[i]["id"], nodes[j]["id"]
                            dx = pos[a][0] - pos[b][0]
                            dy = pos[a][1] - pos[b][1]
                            d2 = max(dx * dx + dy * dy, 1.0)
                            f = rep / d2
                            force[a] = (force[a][0] + f * dx, force[a][1] + f * dy)
                            force[b] = (force[b][0] - f * dx, force[b][1] - f * dy)
                    for a, b in edges:
                        dx = pos[b][0] - pos[a][0]
                        dy = pos[b][1] - pos[a][1]
                        d = max((dx * dx + dy * dy) ** 0.5, 1.0)
                        f = k * (d - c0)
                        force[a] = (force[a][0] + f * dx / d, force[a][1] + f * dy / d)
                        force[b] = (force[b][0] - f * dx / d, force[b][1] - f * dy / d)
                    for nd in nodes:
                        x, y = pos[nd["id"]]
                        x = x + force[nd["id"]][0] + (50 - x) * 0.01
                        y = y + force[nd["id"]][1] + (50 - y) * 0.01
                        pos[nd["id"]] = (x, y)
                self.positions = pos
            else:
                self.positions = {}
        return nodes, edges, self.positions, by_id


# Shared instance so the force-layout cache persists across frames.
_GRAPH = Graph()


def _bresenham(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    cells = []
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        cells.append((x0, y0))
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy
    return cells


def build_call_canvas(state: dict, ui: dict, cw: int, ch: int) -> list[str]:
    nodes, edges, positions, by_id = _GRAPH.layout(state, "call")
    if not nodes:
        return [col("  no interfaces yet", fg="gray")]

    # scale 0-100 world coords into the canvas, keeping margins for labels
    xs = [positions[n["id"]][0] for n in nodes if n["id"] in positions]
    ys = [positions[n["id"]][1] for n in nodes if n["id"] in positions]
    if not xs:
        return [col("  layout unavailable", fg="gray")]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    margin = 4

    def to_cell(nid: str) -> tuple[int, int]:
        x, y = positions[nid]
        cx = int(margin + (x - min_x) / span_x * (cw - 2 * margin - 1))
        cy = int(margin + (y - min_y) / span_y * (ch - 2 * margin - 1))
        return max(0, min(cw - 1, cx)), max(0, min(ch - 1, cy))

    cells_pos = {n["id"]: to_cell(n["id"]) for n in nodes}
    # canvas: char grid + style overlay map per cell
    grid = [[" " for _ in range(cw)] for _ in range(ch)]
    styles: dict[tuple[int, int], str] = {}

    # edges
    sel = ui.gsel
    sel_id = nodes[sel % len(nodes)]["id"] if nodes else None
    edge_cells: dict[tuple[int, int], tuple[str, str]] = {}
    for a, b in edges:
        if a not in cells_pos or b not in cells_pos:
            continue
        (ax, ay), (bx, by) = cells_pos[a], cells_pos[b]
        hot = sel_id in (a, b)
        for (x, y) in _bresenham(ax, ay, bx, by):
            if (x, y) in cells_pos.values():
                continue
            if (x, y) in edge_cells:
                continue
            char = "─" if abs(bx - ax) >= abs(by - ay) else "│"
            edge_cells[(x, y)] = (char, "hot" if hot else "dim")

    # render edges
    for (x, y), (char, kind) in edge_cells.items():
        grid[y][x] = char
        styles[(x, y)] = ("cyan" if kind == "hot" else "gray") + (",bold" if kind == "hot" else ",dim")

    # node labels
    for n in nodes:
        x, y = cells_pos[n["id"]]
        label = n["id"]
        start = max(0, min(cw - len(label), x - len(label) // 2))
        fg = type_color(n.get("type"))
        style = (fg or "white") + (",bold" if n.get("implemented") else "")
        if n["id"] == sel_id:
            style += ",rev"
        for i, ch in enumerate(label):
            cx = start + i
            if cx >= cw:
                break
            grid[y][cx] = ch
            styles[(cx, y)] = style

    lines = ["".join(row) for row in grid]
    # apply styles by composing segments
    styled: list[str] = []
    for y, row in enumerate(lines):
        seg = ""
        cur_style = None
        cur_text = ""
        for x, ch in enumerate(row):
            s = styles.get((x, y))
            if s != cur_style:
                if cur_text:
                    seg += _apply(cur_text, cur_style)
                cur_text = ch
                cur_style = s
            else:
                cur_text += ch
        if cur_text:
            seg += _apply(cur_text, cur_style)
        styled.append(seg)
    return styled


def _apply(text: str, style: str | None) -> str:
    if not style:
        return text
    parts = style.split(",")
    return col(text, fg=parts[0], bold="bold" in parts, dim="dim" in parts, rev="rev" in parts)


def build_tree_canvas(state: dict, width: int) -> list[Row]:
    nodes = state["nodes"]
    by_id = {n["id"]: n for n in nodes}
    if "ROOT" not in by_id:
        by_id["ROOT"] = {"id": "ROOT", "name": "ROOT", "state": None, "parent": None,
                         "children": [n["id"] for n in nodes if (n.get("parent") == "ROOT" or not n.get("parent"))]}
    rows: list[Row] = []
    name_w = max(16, width - 30)
    visited: set[str] = set()
    order_idx = {n["id"]: i for i, n in enumerate(nodes)}

    def walk(nid: str, prefix: str, is_last: bool) -> None:
        visited.add(nid)
        n = by_id[nid]
        st = n.get("state")
        fg, bold = state_color(st) if st else (None, False)
        stem = "└── " if is_last else "├── "
        name = (n.get("name") or nid)[:name_w]
        state_str = f"[{st}]" if st else ""
        line = f"  {prefix}{col(stem, fg='gray')}{col(nid, bold=True)}"
        if name and name != nid:
            line += col("  " + name, fg="gray")
        if state_str:
            line += "  " + col(state_str, fg=fg, bold=bold)
        rows.append(Row(line, kind="node", id=nid))
        kids = [c for c in (n.get("children") or []) if c in by_id]
        kids.sort(key=lambda c: order_idx.get(c, 10**9))
        if not kids:
            return
        next_prefix = prefix + ("    " if is_last else "│   ")
        for i, c in enumerate(kids):
            walk(c, next_prefix, i == len(kids) - 1)

    walk("ROOT", "", True)
    # Nodes whose parent chain never reaches ROOT (missing/dangling parent
    # references) would otherwise vanish — surface them as extra roots.
    for nid in by_id:
        if nid not in visited:
            walk(nid, "", True)
    return rows


def render_graph(state: dict, ui: "UIState", width: int, height: int) -> list[str]:
    head = header_lines(state, ui.paused, width)
    mode = ui.gmode
    out = list(head)
    mode_label = "requirement tree" if mode == "tree" else "interface call graph"
    out.append(col(f"  graph: {mode_label}", fg="gray"))
    body_h = max(1, height - len(out) - 1)

    if mode == "tree":
        rows = build_tree_canvas(state, width)
        selectable = [i for i, r in enumerate(rows) if r.kind]
        sel = ui.gsel % len(selectable) if selectable else 0
        sel_row = selectable[sel] if selectable else -1
        scroll = ui.gr_scroll
        if sel_row >= 0:
            if scroll > sel_row:
                scroll = sel_row
            elif sel_row >= scroll + body_h:
                scroll = sel_row - body_h + 1
        scroll = min(scroll, max(0, len(rows) - body_h))
        ui.gr_scroll = scroll
        for i in range(scroll, min(scroll + body_h, len(rows))):
            r = rows[i]
            out.append(col(r.line, rev=True) if i == sel_row else r.line)
        while len(out) < height:
            out.append("")
        if selectable:
            out[height - 1] = col(f"  {sel + 1}/{len(selectable)} · Enter detail · 3/4 switch", fg="gray")
    else:
        cw = max(20, width - 2)
        ch = max(6, body_h - 2)
        lines = build_call_canvas(state, ui, cw, ch)
        for line in lines[:body_h]:
            out.append(line)
        while len(out) < height:
            out.append("")
        if ui.call_nodes:
            sel = ui.gsel % len(ui.call_nodes)
            nid = ui.call_nodes[sel]["id"]
            info = f"  {col(nid, bold=True)}"
            node = next((i for i in state["interfaces"] if i["interface_id"] == nid), None)
            if node:
                if node.get("implemented"):
                    info += col("  [implemented]", fg="green")
                callees = [c for c in (node.get("callees") or []) if c != nid]
                if callees:
                    info += "  callees: " + ", ".join(callees[:6])
            out[height - 1] = info[:width]
    return out


# ---------------------------------------------------------------------------
# Rendering — detail page
# ---------------------------------------------------------------------------

def build_detail_lines(d: dict, width: int) -> list[str]:
    out: list[str] = []

    def add(text: str) -> None:
        out.extend(wrap_text(text, max(20, width - 4)))

    def section(title: str) -> None:
        out.append("")
        out.append(col(f"── {title} ─{'─' * max(0, width - len(title) - 8)}", fg="gray"))

    if d.get("error"):
        out.append(col(f"  error: {d['error']}", fg="red"))
        return out

    if d["kind"] == "node":
        e = d.get("entry") or {}
        section("requirement")
        add(f"id:         {d['id']}")
        add(f"name:       {e.get('name') or ''}")
        st = d.get("state") or {}
        add(f"state:      {st.get('state') or 'UNSEEN'}  phase: {st.get('phase') or '—'}  updated: {st.get('updated_at') or '—'}")
        add(f"children:   {', '.join(e.get('children_ids') or []) or 'none'}")
        add(f"dependencies: {', '.join(e.get('dependencies') or []) or 'none'}")
        if e.get("description"):
            section("description")
            add(str(e["description"]))
        scenarios = e.get("scenarios") or []
        if scenarios:
            section(f"scenarios ({len(scenarios)})")
            for sc in scenarios:
                out.append(col(f"  · {sc.get('name', '')}", bold=True))
                for step in sc.get("steps") or []:
                    kw = step.get("keyword", "")
                    kfg = {"GIVEN": "magenta", "WHEN": "yellow", "THEN": "green"}.get(kw, "gray")
                    out.append(f"    {col(kw.ljust(6), fg=kfg, bold=True)}{step.get('content', '')}")
        if e.get("visual_reference"):
            section("visual reference analysis")
            add("\n\n".join(str(v) for v in e["visual_reference"]))
        ps = d.get("session_summary") or {}
        if ps.get("phase_status"):
            section("session phase status")
            add(json.dumps(ps["phase_status"], indent=2, ensure_ascii=False))
        if ps.get("recent_failure_summary"):
            section("recent failure summary")
            add(str(ps["recent_failure_summary"]))
        section("full raw JSON")
        add(json.dumps(e, indent=2, ensure_ascii=False))
        out.append("")
        out.append(col("  press s → full node session   (if available)", fg="gray"))
        return out

    if d["kind"] == "interface":
        rec = d.get("record") or {}
        c = d.get("content_parsed") or {}
        section("interface")
        add(f"id:           {d['id']}")
        add(f"type:         {rec.get('type')}")
        add(f"file:         {rec.get('file_path')}:{rec.get('first_line') or '1'}")
        add(f"implemented:  {'yes' if rec.get('implemented') else 'no'}")
        add(f"requirement owners: {', '.join(rec.get('req_ids') or [])}")
        if c.get("name"):
            section("name")
            add(str(c["name"]))
        for field in ("responsibility", "specification"):
            if c.get(field):
                section(field)
                add(str(c[field]))
        for field in ("inputs", "outputs"):
            if c.get(field):
                section(field)
                val = c[field]
                add(val if isinstance(val, str) else json.dumps(val, indent=2, ensure_ascii=False))
        if c.get("callers"):
            section("callers")
            add("\n".join(str(x) for x in c["callers"]))
        if c.get("callees"):
            section("callees")
            add("\n".join(str(x) for x in c["callees"]))
        if c.get("test_focus"):
            section("test focus")
            val = c["test_focus"]
            add(val if isinstance(val, str) else json.dumps(val, indent=2, ensure_ascii=False))
        section("full raw JSON")
        add(json.dumps(rec, indent=2, ensure_ascii=False))
        return out

    if d["kind"] == "test":
        rec = d.get("record") or {}
        section("test")
        add(f"id:            {d['id']}")
        add(f"type:          {rec.get('type')}")
        add(f"file:          {rec.get('file_path')}:{rec.get('first_line') or '1'}")
        passed = rec.get("passed")
        add(f"result:        {'PASS' if passed is True else 'FAIL' if passed is False else 'pending'}")
        add(f"requirement:   {rec.get('req_id') or ''}")
        add(f"interfaces covered: {', '.join(rec.get('interface_ids') or []) or 'none'}")
        section("full raw JSON")
        add(json.dumps(rec, indent=2, ensure_ascii=False))
        return out

    if d["kind"] == "session":
        section("node session")
        add(d.get("pretty") or d.get("error") or "")
        return out

    if d["kind"] == "event":
        section("raw event line")
        add(d.get("raw") or d.get("error") or "")
        section("pretty JSON")
        add(d.get("pretty") or "")
        return out

    section("detail")
    add(json.dumps(d, indent=2, ensure_ascii=False))
    return out


def render_detail(d: dict, ui: "UIState", width: int, height: int) -> list[str]:
    lines = build_detail_lines(d, width)
    body_h = max(1, height - 3)
    scroll = min(ui.det_scroll, max(0, len(lines) - body_h))
    ui.det_scroll = scroll
    title = f" {d.get('kind', '')} · {d.get('id', '')}"
    if d.get("kind") == "interface":
        name = (d.get("content_parsed") or {}).get("name")
    elif d.get("kind") == "node":
        name = (d.get("entry") or {}).get("name")
    else:
        name = None
    if name:
        title += f"  —  {name}"
    if ui.paused:
        title += col("   PAUSED", fg="yellow", bold=True)
    out = [
        col(title, bold=True, fg="blue"),
        col("  Esc back · ↑↓/j k/u d/pgup/pgdn scroll · s node session", fg="gray"),
    ]
    for i in range(scroll, min(scroll + body_h, len(lines))):
        out.append(lines[i])
    while len(out) < height:
        out.append("")
    pct = min(100, round(100 * (scroll + body_h) / max(len(lines), 1)))
    out[height - 1] = col(f"  {scroll + 1}-{min(scroll + body_h, len(lines))} of {len(lines)}  ({pct}%)", fg="gray")
    return out


# ---------------------------------------------------------------------------
# TUI loop
# ---------------------------------------------------------------------------

def state_signature(state: dict) -> str:
    parts = [
        str(state.get("runner")),
        str([(t.get("order"), t.get("status"), t.get("node_id")) for t in state["queue"]["tasks"]]),
        str([(n["id"], n.get("state"), n.get("phase")) for n in state["nodes"]]),
        str([(i["interface_id"], i["implemented"]) for i in state["interfaces"]]),
        str([(t["test_id"], t["passed"]) for t in state["tests"]]),
        str([(e.get("line"), e.get("type")) for e in state["events"]]),
        str(state["log"][-1]) if state["log"] else "",
        str(len(state["log"])),
    ]
    return "|".join(parts)


def snapshot(state: dict) -> str:
    width, _ = terminal_size()
    head = header_lines(state, False, width)
    rows = build_overview(state, width)
    out = [col("ARC monitor — snapshot", bold=True, fg="blue")]
    out.extend(head[1:])
    out.extend(r.line for r in rows)
    return "\n".join(out)


@dataclass
class UIState:
    """Mutable TUI state shared between the input loop and the renderers."""

    screen: str = "overview"                     # overview | graph | detail
    gmode: str = "tree"                          # tree | call
    sel: int = 0                                 # overview selection index
    gsel: int = 0                                # graph selection index
    ov_scroll: int = 0                           # per-screen scroll positions
    gr_scroll: int = 0
    det_scroll: int = 0
    follow: bool = False                         # stick overview to newest log lines
    paused: bool = False
    call_nodes: list = field(default_factory=list)   # call-graph node order
    detail: dict | None = None                   # current detail payload
    detail_stack: list = field(default_factory=list)  # [(kind, id)] for Esc-back
    screen_under: str = "overview"               # screen to return to after detail


def run_tui(monitor: Monitor) -> int:
    inp = Input()
    inp.enter()
    atexit.register(inp.leave)
    resize = {"flag": False}

    def on_winch(signum, frame):  # noqa: ARG001
        resize["flag"] = True

    signal.signal(signal.SIGWINCH, on_winch)

    ui = UIState()
    state: dict | None = None
    last_sig: str | None = None
    last_fetch = 0.0
    dirty = True
    first = True

    hide_cursor()
    atexit.register(show_cursor)
    try:
        while True:
            # --- input ---
            while True:
                key = inp.read_key(0.0)
                if key is None:
                    break
                if key in ("q", "Q"):
                    return 0
                if key in ("p", "P"):
                    ui.paused = not ui.paused
                    dirty = True
                    continue
                if ui.screen == "detail":
                    d = ui.detail
                    if d is None:
                        ui.screen = ui.screen_under
                        continue
                    lines = build_detail_lines(d, terminal_size()[0])
                    body_h = max(1, terminal_size()[1] - 3)
                    if key in ("up", "k"):
                        ui.det_scroll = max(0, ui.det_scroll - 1)
                    elif key in ("down", "j"):
                        ui.det_scroll = min(max(0, len(lines) - body_h), ui.det_scroll + 1)
                    elif key in ("u", "U"):
                        ui.det_scroll = max(0, ui.det_scroll - max(1, body_h // 2))
                    elif key in ("d", "D"):
                        ui.det_scroll = min(max(0, len(lines) - body_h), ui.det_scroll + max(1, body_h // 2))
                    elif key == "pgup":
                        ui.det_scroll = max(0, ui.det_scroll - body_h)
                    elif key == "pgdn":
                        ui.det_scroll = min(max(0, len(lines) - body_h), ui.det_scroll + body_h)
                    elif key in ("home", "g"):
                        ui.det_scroll = 0
                    elif key in ("end", "G"):
                        ui.det_scroll = max(0, len(lines) - body_h)
                    elif key in ("s", "S") and d.get("kind") == "node":
                        ui.detail_stack.append(("node", d["id"]))
                        ui.detail = monitor.detail("session", d["id"])
                        ui.det_scroll = 0
                    elif key in ("esc", "backspace"):
                        if ui.detail_stack:
                            kind, item_id = ui.detail_stack.pop()
                            ui.detail = monitor.detail(kind, item_id)
                            ui.det_scroll = 0
                        else:
                            ui.screen = ui.screen_under
                            ui.detail = None
                            ui.det_scroll = 0
                    elif key in ("1", "2"):
                        ui.screen_under = "overview" if key == "1" else "graph"
                        ui.screen = ui.screen_under
                        ui.detail = None
                        ui.detail_stack = []
                        ui.det_scroll = 0
                    dirty = True
                    continue
                # --- main screens ---
                if key in ("1", "2"):
                    ui.screen = "overview" if key == "1" else "graph"
                    ui.sel = 0
                    ui.gsel = 0
                    dirty = True
                elif key in ("3", "4") and ui.screen == "graph":
                    ui.gmode = "tree" if key == "3" else "call"
                    ui.gsel = 0
                    dirty = True
                elif ui.screen == "overview":
                    rows = build_overview(state, terminal_size()[0]) if state else []
                    nrows = len(rows)
                    if key in ("down", "j", "tab"):
                        if nrows:
                            ui.sel = (ui.sel + 1) % nrows
                        ui.follow = False
                    elif key in ("up", "k", "shift-tab"):
                        if nrows:
                            ui.sel = (ui.sel - 1) % nrows
                        ui.follow = False
                    elif key in ("home", "g"):
                        ui.sel = 0
                        ui.follow = False
                    elif key in ("end", "G"):
                        ui.sel = nrows - 1 if nrows else 0
                        ui.follow = False
                    elif key == "enter" and nrows:
                        target = ui.sel % nrows
                        r = rows[target]
                        if not r.kind:
                            # snap forward (wrapping) to the next inspectable row
                            for step in range(1, nrows + 1):
                                probe = rows[(target + step) % nrows]
                                if probe.kind:
                                    target = (target + step) % nrows
                                    r = probe
                                    break
                        if r.kind:
                            ui.sel = target
                            ui.detail = monitor.detail(r.kind, r.id)
                            ui.screen_under = "overview"
                            ui.detail_stack = []
                            ui.screen = "detail"
                            ui.det_scroll = 0
                    elif key in ("f", "F"):
                        ui.follow = not ui.follow
                    dirty = True
                elif ui.screen == "graph":
                    if ui.gmode == "tree":
                        rows = build_tree_canvas(state, terminal_size()[0]) if state else []
                        selectable = [i for i, r in enumerate(rows) if r.kind]
                        nsel = len(selectable)
                        if key in ("down", "j", "tab"):
                            if nsel:
                                ui.gsel = (ui.gsel + 1) % nsel
                        elif key in ("up", "k", "shift-tab"):
                            if nsel:
                                ui.gsel = (ui.gsel - 1) % nsel
                        elif key == "enter" and nsel:
                            r = rows[selectable[ui.gsel % nsel]]
                            ui.detail = monitor.detail("node", r.id)
                            ui.screen_under = "graph"
                            ui.detail_stack = []
                            ui.screen = "detail"
                            ui.det_scroll = 0
                    else:
                        nn = len(ui.call_nodes)
                        if key in ("down", "right", "tab"):
                            if nn:
                                ui.gsel = (ui.gsel + 1) % nn
                        elif key in ("up", "left", "shift-tab"):
                            if nn:
                                ui.gsel = (ui.gsel - 1) % nn
                        elif key == "enter" and nn:
                            nid = ui.call_nodes[ui.gsel % nn]["id"]
                            ui.detail = monitor.detail("interface", nid)
                            ui.screen_under = "graph"
                            ui.detail_stack = []
                            ui.screen = "detail"
                            ui.det_scroll = 0
                    dirty = True

            # --- refresh ---
            now = time.monotonic()
            if not ui.paused and now - last_fetch >= 1.0:
                last_fetch = now
                new_state = monitor.state()
                sig = state_signature(new_state)
                if sig != last_sig:
                    state = new_state
                    last_sig = sig
                    dirty = True
            if ui.screen == "graph" and ui.gmode == "call" and state:
                if not ui.call_nodes or state_signature(state) != last_sig:
                    nodes, _, _, _ = _GRAPH.layout(state, "call")
                    ui.call_nodes = nodes

            # --- render ---
            if dirty or resize["flag"]:
                resize["flag"] = False
                dirty = False
                width, height = terminal_size()
                if state is None:
                    frame = [" " * width] * height
                    frame[height // 2] = col("  waiting for workspace data…", fg="gray")
                elif ui.screen == "detail" and ui.detail is not None:
                    frame = render_detail(ui.detail, ui, width, height)
                elif ui.screen == "graph":
                    frame = render_graph(state, ui, width, height)
                else:
                    frame = render_overview(state, ui, width, height)
                for i in range(min(len(frame), height)):
                    line = frame[i]
                    if len(line) > width:
                        line = line[:width]
                    elif len(line) < width:
                        line = line + " " * (width - len(line))
                    frame[i] = line
                out = "\x1b[H\x1b[J" + "\n".join(frame)
                if first:
                    first = False
                    out = "\x1b[2J" + out
                sys.stdout.write(out)
                sys.stdout.flush()
            else:
                inp.pending(min(0.05, max(0.0, 1.0 - (time.monotonic() - last_fetch))))
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
        prog=prog or "arc monitor",
        description="Terminal progress monitor for an ARC compilation workspace.",
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

    monitor = Monitor(workspace)

    if not sys.stdout.isatty():
        print(snapshot(monitor.state()))
        return 0

    print(f"watching {workspace}", flush=True)
    try:
        return run_tui(monitor)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
