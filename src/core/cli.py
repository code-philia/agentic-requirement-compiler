from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from colorama import Fore, Style, init as colorama_init

from core.logging import append_debug_log, write_terminal_log

colorama_init()
# ======================================================================================
# CLI progress view
# ======================================================================================


def _format_time() -> str:
    return time.strftime("%H:%M:%S")


class _Spinner:
    FRAMES = [".  ", ".. ", "...", " ..", "  .", "   "]

    def __init__(self) -> None:
        self._active = False
        self._thread: threading.Thread | None = None
        self._text = ""
        self._last_width = 0

    def start(self, text: str) -> None:
        if self._active:
            self._text = text
            return
        self._active = True
        self._text = text
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._active = False
        if self._thread:
            self._thread.join(timeout=0.5)
        width = max(self._last_width, 100)
        sys.stdout.write("\r" + " " * width + "\r")
        sys.stdout.flush()
        self._last_width = 0

    def _spin(self) -> None:
        index = 0
        while self._active:
            dots = self.FRAMES[index % len(self.FRAMES)]
            line = (
                f"\r{_format_time()}  {self._label()} "
                f"{Fore.CYAN}Working{dots}{Style.RESET_ALL} {Fore.WHITE}{self._detail()}{Style.RESET_ALL}"
            )
            plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
            self._last_width = max(self._last_width, len(plain))
            sys.stdout.write(line)
            sys.stdout.flush()
            index += 1
            time.sleep(0.25)

    def _parts(self) -> list[str]:
        return [part.strip() for part in self._text.split("|") if part.strip()]

    def _label(self) -> str:
        parts = self._parts()
        node = parts[0] if parts else "ARC"
        if len(parts) >= 3 and parts[1] in {"Design", "Tests", "Implement"}:
            agent = parts[2]
        elif len(parts) >= 2:
            agent = parts[1]
        else:
            agent = "System"
        return f"{Fore.MAGENTA}{agent.replace(' ', ''):<20}{Style.RESET_ALL} {Fore.CYAN}{node:<10}{Style.RESET_ALL}"

    def _detail(self) -> str:
        parts = self._parts()
        if len(parts) >= 3 and parts[1] in {"Design", "Tests", "Implement"}:
            return parts[1]
        if len(parts) >= 3:
            return parts[2]
        return self._text


class _CliProgressView:
    PHASE_LABELS = {"DESIGN": "Design", "IMPLEMENT": "Implement"}

    def __init__(self) -> None:
        self.current_node_id: str | None = None
        self.current_phase: str | None = None
        self._last_line: str | None = None
        self._transient_line_count = 0
        self._tool_batch_lines: list[str] = []
        self._tool_batch_open = False

    @staticmethod
    def _plain_node(node_id: str | None) -> str:
        return str(node_id or "ARC").strip() or "ARC"

    @staticmethod
    def _pretty_agent(agent: str) -> str:
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", str(agent or ""))
        return " ".join(part if part.isupper() else part.capitalize() for part in parts) or str(agent or "System")

    @staticmethod
    def _compact_path(path: str) -> str:
        normalized = str(path or "").replace("\\", "/").strip().rstrip("/")
        if not normalized:
            return ""
        parts = [part for part in normalized.split("/") if part]
        if len(normalized) <= 64:
            return normalized
        return "/".join(parts[-4:])

    @staticmethod
    def _clip(text: str, limit: int = 96) -> str:
        value = " ".join(str(text or "").split())
        return value if len(value) <= limit else value[: limit - 3] + "..."

    def _meta_prefix(self, agent: str, node_id: str | None) -> str:
        return (
            f"{Fore.BLUE}{_format_time()}{Style.RESET_ALL}  "
            f"{Fore.MAGENTA}{self._agent_label(agent):<20}{Style.RESET_ALL} "
            f"{Fore.CYAN}{self._plain_node(node_id):<10}{Style.RESET_ALL}"
        )

    def _agent_label(self, agent: str) -> str:
        pretty = self._pretty_agent(agent).replace(" ", "")
        return self._clip(pretty or "System", 20)

    def _summarize_tool_args(self, args_text: str) -> str:
        raw = str(args_text or "").strip()
        if not raw:
            return ""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return self._clip(raw, 120)
        if not isinstance(payload, dict):
            return self._clip(json.dumps(payload, ensure_ascii=False), 120)
        path = str(payload.get("path") or payload.get("file_path") or payload.get("cwd") or "").strip()
        command = str(payload.get("command") or "").strip()
        pattern = str(payload.get("pattern") or "").strip()
        todos = payload.get("todos")
        if command:
            detail = f"{command}"
            if path:
                detail += f" @ {self._compact_path(path)}"
            return self._clip(detail, 120)
        if path and pattern:
            return self._clip(f"{pattern} in {self._compact_path(path)}", 120)
        if path:
            return self._compact_path(path)
        if isinstance(todos, list):
            return f"{len(todos)} todo item(s)"
        keys = ", ".join(list(payload.keys())[:4])
        return self._clip(keys, 120)

    def _render_tool_call_event(self, agent: str, message: str, active_node: str | None, stage_line: Callable[[str, str, str], str]) -> str | None:
        match = re.match(r"tool-call>\s*([^\s]+)\s+args=(.*)", message, flags=re.DOTALL)
        if not match:
            return None
        tool_name = match.group(1).strip() or "unknown"
        detail = self._summarize_tool_args(match.group(2))
        tool_line = stage_line("Tool Call", f"{tool_name} {detail}".strip(), "info")
        return self._emit_once(tool_line)

    def _render_tool_result_event(self, message: str, stage_line: Callable[[str, str, str], str]) -> str | None:
        match = re.match(r"tool-result>\s*([^\s]+)\s+result=(.*)", message, flags=re.DOTALL)
        if not match:
            return None
        tool_name = match.group(1).strip() or "unknown"
        result = self._clip(match.group(2), 140)
        result_line = stage_line("Tool Result", f"{tool_name} -> {result}", "ok")
        return self._emit_once(result_line)

    def _render_tool_batch_event(self, message: str, stage_line: Callable[[str, str, str], str]) -> str | None:
        raw = message.split("tool-batch>", 1)[1].strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return stage_line("Tool Batch", self._clip(raw, 240), "warn")
        tools = payload.get("tools") if isinstance(payload, dict) else []
        if not isinstance(tools, list) or not tools:
            return None

        lines = [stage_line("Tool Batch", f"{len(tools)} completed call(s)", "info")]
        for index, tool in enumerate(tools, start=1):
            if not isinstance(tool, dict):
                continue
            name = self._clip(str(tool.get("name", "") or "unknown"), 48)
            args = self._clip(str(tool.get("args", "") or ""), 280)
            result = self._clip(str(tool.get("result", "") or ""), 360)
            lines.append(stage_line("", f"{index}. {name}"))
            lines.append(stage_line("", f"   args: {args or '{}'}"))
            lines.append(stage_line("", f"   result: {result or '[empty]'}", "ok"))
        return self._emit_once("\n".join(lines))

    def _render_stream_messages(self, message: str, stage_line: Callable[[str, str, str], str]) -> str | None:
        body = message.split("stream messages:", 1)[1] if "stream messages:" in message else message
        lines: list[str] = []
        current_role = ""
        current_tool = ""
        current_id = ""
        current_args = ""
        current_content: list[str] = []
        reading_args = False
        reading_content = False

        def flush() -> None:
            nonlocal current_role, current_tool, current_id, current_args, current_content, reading_args, reading_content
            if current_role == "TOOL":
                content = self._clip(" ".join(current_content), 140)
                name_part = f"{current_tool} " if current_tool else ""
                id_part = current_id[-8:] if current_id else ""
                label = f"{name_part}{id_part}".strip() or "tool"
                result_line = stage_line("Tool Result", f"{label} -> {content}", "ok")
                self._emit_transient_tool(result_line)
            elif current_tool:
                detail = self._summarize_tool_args(current_args)
                tool_line = stage_line("Tool Call", f"{current_tool} {detail}".strip(), "info")
                self._emit_transient_tool(tool_line)
            elif current_role == "AI" and current_content:
                content = self._clip(" ".join(current_content), 160)
                if content and not content.startswith("{\"type\": \"tool_call\""):
                    lines.append(stage_line("Model", content, "agent"))
            current_role = ""
            current_tool = ""
            current_id = ""
            current_args = ""
            current_content = []
            reading_args = False
            reading_content = False

        for raw_line in body.splitlines():
            stripped = raw_line.strip()
            if re.match(r"\[\d+\]\s+(AI|TOOL|HUMAN|ASSISTANT)", stripped):
                flush()
                current_role = stripped.split("]", 1)[1].strip().upper()
                continue
            tool_call = re.match(r"\d+\.\s+([A-Za-z0-9_.-]+|unknown)\s+id=([A-Za-z0-9_-]+)", stripped)
            if tool_call and current_role == "AI":
                flush()
                current_role = "AI"
                current_tool = tool_call.group(1)
                current_id = tool_call.group(2)
                continue
            result_meta = re.match(r"tool_result:\s*(?:name=([^\s]+)\s*)?id=([A-Za-z0-9_-]+)", stripped)
            if result_meta:
                current_role = "TOOL"
                current_tool = result_meta.group(1) or ""
                current_id = result_meta.group(2)
                continue
            if stripped.startswith("args:"):
                reading_args = True
                reading_content = False
                current_args = stripped.split("args:", 1)[1].strip()
                continue
            if stripped.startswith("content:"):
                reading_content = True
                reading_args = False
                content = stripped.split("content:", 1)[1].strip()
                if content:
                    current_content.append(content)
                continue
            if reading_args and stripped:
                current_args += stripped
                continue
            if reading_content and stripped:
                current_content.append(stripped)
        flush()
        self._close_tool_batch()
        if not lines:
            return None
        return self._emit_once("\n".join(lines))

    def _render_artifact_panel(
        self,
        *,
        title: str,
        message: str,
        prefix: str,
        meta: str,
        kind: str,
    ) -> str | None:
        raw = message.split(prefix, 1)[1].strip() if prefix in message else ""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        items = payload.get("items") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            items = []

        width = 78
        border = f"{Fore.BLUE}{'─' * width}{Style.RESET_ALL}"
        lines = [
            "",
            f"{meta}  {border}",
            f"{meta}  {Fore.GREEN}{title:<12}{Style.RESET_ALL} {Fore.WHITE}{len(items)} {kind}(s){Style.RESET_ALL}",
            f"{meta}  {border}",
        ]
        if not items:
            lines.append(f"{meta}  {Fore.YELLOW}none{Style.RESET_ALL}         {Fore.WHITE}No node-local {kind}s recorded.{Style.RESET_ALL}")
        for index, item in enumerate(items[:12], start=1):
            if not isinstance(item, dict):
                continue
            item_id = self._clip(str(item.get("id", "") or ""), 34)
            item_type = self._clip(str(item.get("type", "") or "?"), 14)
            path = self._compact_path(str(item.get("path", "") or ""))
            lines.append(
                f"{meta}  {Fore.CYAN}{index:>2}.{Style.RESET_ALL} {Fore.WHITE}{item_id}{Style.RESET_ALL} "
                f"{Fore.MAGENTA}{item_type}{Style.RESET_ALL} {Fore.WHITE}{path}{Style.RESET_ALL}"
            )
            if kind == "interface":
                responsibility = self._clip(str(item.get("responsibility", "") or ""), 96)
                if responsibility:
                    lines.append(f"{meta}      {Fore.WHITE}{responsibility}{Style.RESET_ALL}")
            else:
                interfaces = item.get("interfaces")
                if isinstance(interfaces, list) and interfaces:
                    covered = ", ".join(str(value) for value in interfaces[:4])
                    lines.append(f"{meta}      {Fore.WHITE}covers: {self._clip(covered, 96)}{Style.RESET_ALL}")
        if len(items) > 12:
            lines.append(f"{meta}      {Fore.YELLOW}... {len(items) - 12} more omitted from terminal view{Style.RESET_ALL}")
        lines.append(f"{meta}  {border}")
        return self._emit_once("\n".join(lines))

    def _emit_once(self, line: str | None) -> str | None:
        if not line or line == self._last_line:
            return None
        self._last_line = line
        return line

    def _clear_transient_block(self) -> str:
        if self._transient_line_count <= 0:
            return ""
        clear = "".join("\033[F\033[2K" for _ in range(self._transient_line_count))
        self._transient_line_count = 0
        return clear

    def clear_transient_block(self) -> None:
        clear = self._clear_transient_block()
        if clear:
            sys.stdout.write(clear)
            sys.stdout.flush()

    def _mark_transient(self, rendered: str | None) -> str | None:
        if not rendered:
            return None
        clear = self._clear_transient_block()
        self._transient_line_count = rendered.count("\n") + 1
        return f"{clear}{rendered}" if clear else rendered

    def _persistent(self, rendered: str | None) -> str | None:
        clear = self._clear_transient_block()
        if not rendered:
            return clear or None
        return f"{clear}{rendered}" if clear else rendered



    def _emit_transient_tool(self, line: str) -> None:
        """Emit a tool call/result line in transient mode (will be cleared on next batch)."""
        if not self._tool_batch_open:
            self._tool_batch_open = True
            self._tool_batch_lines = []
        
        self._tool_batch_lines.append(line)
        
        # Clear previous tool batch
        if self._transient_line_count > 0:
            clear = "".join("\033[F\033[2K" for _ in range(self._transient_line_count))
            sys.stdout.write(clear)
            sys.stdout.flush()
        
        # Print entire batch
        for batch_line in self._tool_batch_lines:
            print(batch_line, flush=True)
        self._transient_line_count = len(self._tool_batch_lines)

    def _close_tool_batch(self) -> None:
        """Finalize tool batch, making it persistent."""
        self._tool_batch_open = False
        self._tool_batch_lines = []
        self._transient_line_count = 0

    def _start_phase_timer(self, node_id: str, phase: str):
        """Start timing a node phase."""
        key = f"{node_id}:{phase}"
        import time
        self._node_start_times[key] = time.time()

    def _stop_phase_timer(self, node_id: str, phase: str) -> str:
        """Stop timing and return elapsed time string."""
        key = f"{node_id}:{phase}"
        if key not in self._node_start_times:
            return ""
        import time
        elapsed = time.time() - self._node_start_times[key]
        del self._node_start_times[key]
        self._node_phase_times[key] = elapsed
        if elapsed < 60:
            return f" {Fore.WHITE}({elapsed:.1f}s){Style.RESET_ALL}"
        mins = int(elapsed / 60)
        secs = int(elapsed % 60)
        return f" {Fore.WHITE}({mins}m{secs:02d}s){Style.RESET_ALL}"

    def render(self, agent: str, message: str, status: str | None = None, node_id: str | None = None) -> str | None:
        active_node = node_id or self.current_node_id

        def stage_line(stage: str, detail: str, tone: str = "info") -> str:
            color = {
                "info": Fore.CYAN,
                "ok": Fore.GREEN,
                "warn": Fore.YELLOW,
                "fail": Fore.RED,
                "agent": Fore.MAGENTA,
                "test": Fore.GREEN,
            }.get(tone, Fore.WHITE)
            return f"{self._meta_prefix(agent, active_node)}  {color}{stage:<12}{Style.RESET_ALL} {Fore.WHITE}{detail}{Style.RESET_ALL}"

        def header(title: str, detail: str) -> str:
            bar = f"{Fore.BLUE}{'=' * 78}{Style.RESET_ALL}"
            meta = self._meta_prefix(agent, active_node)
            return f"\n{meta}  {bar}\n{meta}  {Fore.CYAN}{title}{Style.RESET_ALL} {Fore.WHITE}{detail}{Style.RESET_ALL}"

        run_match = re.match(r"Running (DESIGN|IMPLEMENT) for node (.+?)\.\.\.", message)
        if agent == "Compiler" and run_match:
            self.current_phase = run_match.group(1)
            self.current_node_id = run_match.group(2).strip()
            phase = self.PHASE_LABELS.get(self.current_phase, self.current_phase.title())
            return self._emit_once(header(f"{self.current_node_id} / {phase}", "starting"))

        done_match = re.match(r"(DESIGN|IMPLEMENT) completed for node (.+?)\.", message)
        if agent == "Compiler" and done_match:
            phase = self.PHASE_LABELS.get(done_match.group(1), done_match.group(1).title())
            done_node = done_match.group(2).strip()
            return self._emit_once(
                f"{self._meta_prefix(agent, done_node)}  {Fore.GREEN}done        {Style.RESET_ALL} {Fore.WHITE}{phase} complete{Style.RESET_ALL}"
            )

        if agent == "Compiler" and message == "ARC compilation started.":
            return self._emit_once(header("ARC", "compilation started"))
        if agent == "Compiler" and message.startswith("Loaded processing queue with"):
            match = re.search(r"with (\d+) task\(s\)", message)
            return self._emit_once(stage_line("Queue", f"{match.group(1) if match else '?'} task(s) scheduled"))
        if agent == "Compiler" and message.startswith("Resuming from existing queue"):
            return self._emit_once(stage_line("Resume", "Continuing from saved queue", "warn"))
        if agent == "Compiler" and message.startswith("Running git checkpoint"):
            return self._emit_once(stage_line("Checkpoint", "Saving milestone"))
        if agent == "Compiler" and message.startswith("Compilation finished successfully"):
            return self._emit_once(f"\n{self._meta_prefix(agent, active_node)}  {Fore.GREEN}SUCCESS     {Style.RESET_ALL} {Fore.WHITE}Compilation finished successfully{Style.RESET_ALL}")
        if agent == "Compiler" and message.startswith("Compilation finished with"):
            return self._emit_once(f"\n{self._meta_prefix(agent, active_node)}  {Fore.RED}FAILED      {Style.RESET_ALL} {Fore.WHITE}{message}{Style.RESET_ALL}")

        if agent == "RequirementLoader" and message.startswith("Reading requirements file:"):
            return self._emit_once(stage_line("Input", "Loading requirement model"))

        if agent == "System" and message.startswith("Initializing project environment"):
            return self._emit_once(stage_line("Workspace", "Preparing runtime workspace"))
        if agent == "System" and message.startswith("Using app_type="):
            return None
        if agent == "System" and message.startswith("Copying template from"):
            return self._emit_once(stage_line("Workspace", "Scaffolding project template"))
        if agent == "System" and message == "Template files copied successfully.":
            return self._emit_once(stage_line("Workspace", "Template ready", "ok"))
        if agent == "System" and message.startswith("Configured web template"):
            return self._emit_once(stage_line("Workspace", "Web stack configured", "ok"))
        if agent == "System" and message.startswith("Installing backend dependencies"):
            return self._emit_once(stage_line("Deps", "Installing backend packages"))
        if agent == "System" and message.startswith("Installing frontend dependencies"):
            return self._emit_once(stage_line("Deps", "Installing frontend packages"))
        if agent == "System" and message.startswith("NPM install success in"):
            target = os.path.basename(message.rsplit(" ", 1)[-1].replace("\\", "/").rstrip("/")) or "target"
            return self._emit_once(stage_line("Deps", f"{target} packages ready", "ok"))
        if agent == "System" and message == "Initializing Git repository...":
            return self._emit_once(stage_line("Checkpoint", "Initializing git history"))
        if "Prerequisite check passed" in message:
            return self._emit_once(stage_line("Check", message.replace("Prerequisite check passed for ", ""), "ok"))
        if "Prerequisite check FAILED" in message or "Missing required command" in message:
            return self._emit_once(stage_line("Check", message, "fail"))
        if agent == "System" and message.startswith("Analyzing visual element:"):
            return self._emit_once(stage_line("Visual", "Analyzing reference image"))
        if agent == "System" and message.startswith("Reusing cached visual analysis:"):
            return self._emit_once(stage_line("Visual", "Reusing cached reference analysis"))
        if agent == "System" and message.startswith("Stored ") and "visual references for" in message:
            return self._emit_once(stage_line("Visual", "Reference analysis stored", "ok"))
        if agent == "System" and message.startswith("System test execution ("):
            test_match = re.match(r"System test execution \(([^)]+)\):\s*(.+)", message)
            if test_match:
                return self._emit_once(stage_line("Verify", f"{test_match.group(1)} :: {self._compact_path(test_match.group(2))}"))
        if agent == "Traceability" and message.startswith(("Query interfaces", "Query interface", "Search interfaces")):
            return None
        if agent == "Compiler" and message == "System is executing build verification.":
            return None

        if agent == "InterfaceDesigner" and message.startswith("Running interface design"):
            return self._emit_once(stage_line("Design", "InterfaceDesigner is shaping interfaces", "agent"))
        if agent == "InterfaceDesigner" and message.startswith("Invoking agent"):
            _spinner.start(f"{self._plain_node(active_node)} | Design | InterfaceDesigner")
            return None
        if agent == "InterfaceDesigner" and message.startswith("Stored ") and "interface definition" in message:
            count = re.search(r"Stored (\d+)", message)
            return self._emit_once(stage_line("Artifacts", f"{count.group(1) if count else '?'} interface definition(s) recorded", "ok"))
        if agent == "InterfaceDesigner" and message.startswith("Interface artifact summary:"):
            return self._persistent(self._render_artifact_panel(
                title="Interfaces",
                message=message,
                prefix="Interface artifact summary:",
                meta=self._meta_prefix(agent, active_node),
                kind="interface",
            ))

        if agent == "TestGenerator" and message.startswith("Generating tests"):
            return self._emit_once(stage_line("Tests", "TestGenerator is generating verification assets", "test"))
        if agent == "TestGenerator" and message.startswith("Invoking agent"):
            _spinner.start(f"{self._plain_node(active_node)} | Tests | TestGenerator")
            return None
        if agent == "TestGenerator" and message.startswith("Stored ") and "test mapping item" in message:
            count = re.search(r"Stored (\d+)", message)
            return self._emit_once(stage_line("Tests", f"{count.group(1) if count else '?'} test mapping item(s) recorded", "ok"))
        if agent == "TestGenerator" and message.startswith("Test artifact summary:"):
            return self._persistent(self._render_artifact_panel(
                title="Tests",
                message=message,
                prefix="Test artifact summary:",
                meta=self._meta_prefix(agent, active_node),
                kind="test",
            ))

        if agent == "TestDrivenDeveloper" and message.startswith("Running TDD batch"):
            return self._emit_once(stage_line("Implement", message, "agent"))
        if agent == "TestDrivenDeveloper" and message.startswith("Invoking agent"):
            _spinner.start(f"{self._plain_node(active_node)} | Implement | TestDrivenDeveloper")
            return None
        if agent == "TestDrivenDeveloper" and message.startswith("`run_tests`") and (" usage " in message or " budget exhausted " in message):
            return self._emit_once(stage_line("Verify", message))
        if agent == "TestDrivenDeveloper" and message.startswith("`run_tests`") and " passed " in message:
            return self._emit_once(stage_line("Verify", message, "ok"))
        if agent == "TestDrivenDeveloper" and message.startswith("`run_tests`") and " failed " in message:
            return self._emit_once(stage_line("Verify", message, "fail"))
        if agent == "TestDrivenDeveloper" and message.startswith("TDD batch") and "passed" in message:
            return self._emit_once(stage_line("Verify", message, "ok"))
        if agent == "TestDrivenDeveloper" and message.startswith("TDD batch") and "did not pass" in message:
            return self._emit_once(stage_line("Verify", message, "fail"))

        if "agent stream start" in message:
            self._close_tool_batch()
            _spinner.start(f"{self._plain_node(active_node)} | {self._pretty_agent(agent)}")
            return None
        if message.startswith("tool-batch>"):
            return self._persistent(self._render_tool_batch_event(message, stage_line))
        if message.startswith("tool-call>"):
            return self._persistent(self._render_tool_call_event(agent, message, active_node, stage_line))
        if message.startswith("tool-result>"):
            return self._persistent(self._render_tool_result_event(message, stage_line))
        if message.startswith("model-final>"):
            content = message.split("model-final>", 1)[1]
            return self._persistent(self._emit_once(stage_line("Model", self._clip(content, 800), "agent")))
        if message.startswith("model>"):
            return self._persistent(self._emit_once(stage_line("Model", self._clip(message.split("model>", 1)[1], 160), "agent")))
        if message.startswith("stream messages:"):
            return self._persistent(self._render_stream_messages(message, stage_line))
        if message.startswith("agent trace:"):
            return None
        if "agent call end" in message:
            self._close_tool_batch()
            return self._persistent(None)

        if status == "error":
            return self._persistent(self._emit_once(stage_line("Error", self._clip(message, 140), "fail")))
        if status == "warning":
            return self._persistent(self._emit_once(stage_line("Note", self._clip(message, 140), "warn")))
        if status == "info":
            return self._persistent(self._emit_once(stage_line("Info", self._clip(message, 140))))

        if agent in {"Compiler", "System", "RequirementLoader", "InterfaceDesigner", "TestGenerator", "TestDrivenDeveloper"}:
            return self._persistent(None)
        return self._persistent(self._emit_once(stage_line(self._pretty_agent(agent), self._clip(message))))


_spinner = _Spinner()
_progress_view = _CliProgressView()
_cli_workspace_root: str | None = None
ARC_DEBUG_ENABLED = str(os.environ.get("ARC_DEBUG", "0")).strip().lower() not in {"0", "false", "no", "off", ""}


def init_debug_logger(project_path: str, reset_existing: bool = True) -> str:
    global _cli_workspace_root
    arc_dir = Path(project_path) / ".arc"
    arc_dir.mkdir(parents=True, exist_ok=True)
    log_path = arc_dir / "debug.log"
    if reset_existing and log_path.exists():
        log_path.unlink()
    os.environ["ARC_DEBUG_LOG_PATH"] = str(log_path)
    _cli_workspace_root = str(Path(project_path).expanduser().resolve())
    return str(log_path)


def stop_cli_spinner() -> None:
    _spinner.stop()


def print_cli_banner() -> None:
    print(
        "\n".join(
            [
                "",
                f"{Fore.BLUE}{'=' * 78}{Style.RESET_ALL}",
                f"{Fore.CYAN}{Style.BRIGHT} ARC {Style.RESET_ALL}{Fore.WHITE}Requirement Compiler{Style.RESET_ALL}",
                f"{Fore.WHITE} Compile requirement graphs into interfaces, tests, and runnable code{Style.RESET_ALL}",
                f"{Fore.BLUE}{'=' * 78}{Style.RESET_ALL}",
            ]
        )
    )


def print_cli_startup(
    project_path: str,
    requirement_path: str,
    app_type: str,
    clear_all: bool,
    log_path: str | None,
    web_port: int | None = None,
    resume_from_queue: bool = False,
    retry_failed: bool = False,
    retry_node_ids: list[str] | None = None,
    model_api_mode: str | None = None,
) -> None:
    from app_type_handler import read_stack_summary

    if retry_failed:
        mode_label = "retry-failed"
    elif retry_node_ids:
        mode_label = "retry-selected"
    elif resume_from_queue:
        mode_label = "resume-compilation"
    elif clear_all:
        mode_label = "clear-and-recompile"
    else:
        mode_label = "start-compilation"
    requirement_name = Path(requirement_path).parent.name or "requirements"
    view_label = "debug" if ARC_DEBUG_ENABLED else "progress"
    print("")
    print(f"{Fore.WHITE} Session{Style.RESET_ALL}")
    print(f"   {Fore.CYAN}mode      {Style.RESET_ALL}{mode_label}")
    print(f"   {Fore.CYAN}input     {Style.RESET_ALL}{requirement_name}")
    print(f"   {Fore.CYAN}output    {Style.RESET_ALL}{project_path}")
    print(f"   {Fore.CYAN}target    {Style.RESET_ALL}{app_type}")
    if retry_failed:
        print(f"   {Fore.CYAN}retry     {Style.RESET_ALL}all failed nodes")
    elif retry_node_ids:
        print(f"   {Fore.CYAN}retry     {Style.RESET_ALL}{', '.join(retry_node_ids)}")
    if app_type == "web" and web_port is not None:
        print(f"   {Fore.CYAN}port      {Style.RESET_ALL}{web_port}")
    if model_api_mode:
        print(f"   {Fore.CYAN}model api {Style.RESET_ALL}{model_api_mode}")
    print(f"   {Fore.CYAN}stack     {Style.RESET_ALL}{read_stack_summary(project_path, app_type)}")
    print(f"   {Fore.CYAN}view      {Style.RESET_ALL}{view_label}")
    if log_path:
        print(f"   {Fore.CYAN}debug log {Style.RESET_ALL}{log_path}")
    print(f"{Fore.BLUE}{'-' * 78}{Style.RESET_ALL}\n")


def cli_log(
    agent_name: str,
    message: str,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    append_debug_log(agent_name, message, status=status, node_id=node_id, workspace_root=_cli_workspace_root)
    if ARC_DEBUG_ENABLED:
        _progress_view.clear_transient_block()
        write_terminal_log(agent_name, message, status=status, node_id=node_id)
        return

    if "agent call end" in message:
        _spinner.stop()
    rendered = _progress_view.render(agent_name, message, status=status, node_id=node_id)
    if rendered:
        _spinner.stop()
        print(rendered, flush=True)
        if message.startswith("tool-result>"):
            _spinner.start(f"{_progress_view._plain_node(node_id)} | {_progress_view._pretty_agent(agent_name)}")


def print_compilation_summary(
    result: dict,
    output_dir: str,
    elapsed_seconds: float,
) -> None:
    """Print enhanced compilation summary with statistics."""
    print(f"\n{Fore.BLUE}{'━' * 78}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}Compilation Summary{Style.RESET_ALL}")
    print(f"{Fore.BLUE}{'━' * 78}{Style.RESET_ALL}\n")
    
    # Time
    if elapsed_seconds < 60:
        time_str = f"{elapsed_seconds:.1f}s"
    else:
        mins = int(elapsed_seconds / 60)
        secs = int(elapsed_seconds % 60)
        time_str = f"{mins}m {secs:02d}s"
    
    print(f"{Fore.WHITE}Duration:{Style.RESET_ALL} {time_str}")
    
    # Nodes
    states = result.get("states", {})
    passed = [nid for nid, st in states.items() if st == "PASSED"]
    failed = [nid for nid, st in states.items() if st == "FAILED"]
    skipped = [nid for nid, st in states.items() if st not in {"PASSED", "FAILED"}]
    
    if passed:
        print(f"{Fore.GREEN}✓ Passed:{Style.RESET_ALL}  {', '.join(passed)}")
    if failed:
        print(f"{Fore.RED}✗ Failed:{Style.RESET_ALL}  {', '.join(failed)}")
    if skipped:
        print(f"{Fore.YELLOW}⊙ Skipped:{Style.RESET_ALL} {', '.join(skipped)}")
    
    # Stats from progress view
    if hasattr(_progress_view, '_phase_stats'):
        stats = _progress_view._phase_stats
        if stats.get("interfaces") or stats.get("tests"):
            print()
            if stats.get("interfaces"):
                print(f"{Fore.CYAN}Interfaces:{Style.RESET_ALL} {stats['interfaces']} designed")
            if stats.get("tests"):
                print(f"{Fore.CYAN}Tests:{Style.RESET_ALL}      {stats['tests']} generated")
    
    # Output
    print(f"\n{Fore.WHITE}Output:{Style.RESET_ALL}    {output_dir}")
    
    debug_log = os.path.join(output_dir, ".arc", "debug.log")
    if os.path.exists(debug_log):
        print(f"{Fore.WHITE}Debug log:{Style.RESET_ALL} {debug_log}")
    
    # Next steps
    if failed:
        print(f"\n{Fore.YELLOW}Next steps:{Style.RESET_ALL}")
        print(f"  • Review failure details in debug log")
        print(f"  • Fix requirement issues and retry: arc compile <input> -o {output_dir} --resume --retry-failed")
    elif result.get("ok"):
        print(f"\n{Fore.GREEN}✓ Compilation successful{Style.RESET_ALL}")
        # TODO: Add app-specific run instructions
    
    print(f"{Fore.BLUE}{'━' * 78}{Style.RESET_ALL}\n")
