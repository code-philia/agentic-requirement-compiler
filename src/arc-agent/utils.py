import os
import re
import shutil
import sys
import time
import yaml
import json
import asyncio
import threading
import copy

from typing import Awaitable, Callable, Optional, Dict, List, Any
from colorama import Fore, Style, init as colorama_init
from runtime_sdk import get_runtime

colorama_init()


def _store():
    return get_runtime().traceability

# ======================================================================================
#                                Runtime Context
# ======================================================================================

WORKSPACE_ROOT = os.getcwd()
APP_TYPE = "web"
ANDROID_PACKAGE = "com.example.template"
WEB_PORT = 3301


def set_workspace_root(path: str) -> None:
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = os.path.abspath(path)


def get_abs_path(rel_path: str) -> str:
    if os.path.isabs(rel_path):
        return rel_path
    return os.path.join(WORKSPACE_ROOT, rel_path)


def set_app_type(app_type: str) -> None:
    global APP_TYPE
    APP_TYPE = (app_type or "web").strip().lower()


def get_app_type() -> str:
    return APP_TYPE


def set_web_port(port: int) -> None:
    global WEB_PORT
    WEB_PORT = int(port)


def get_web_port() -> int:
    return WEB_PORT


def get_web_base_url(host: str = "127.0.0.1") -> str:
    return f"http://{host}:{get_web_port()}"


def build_web_runtime_env(host: str = "127.0.0.1") -> dict[str, str]:
    port = str(get_web_port())
    base_url = get_web_base_url(host)
    return {
        "PORT": port,
        "ARC_WEB_PORT": port,
        "ARC_WEB_BASE_URL": base_url,
        "PLAYWRIGHT_BASE_URL": base_url,
    }


def set_android_package(package_name: str) -> None:
    global ANDROID_PACKAGE
    ANDROID_PACKAGE = package_name.strip()


def get_android_package() -> str:
    return ANDROID_PACKAGE


def read_json_file(path: str) -> dict[str, Any] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return None

def write_json_file(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def get_node_session_dir() -> str:
    return get_abs_path(os.path.join(".arc", "node_sessions"))


def get_node_session_path(node_id: str) -> str:
    safe_node_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(node_id or "").strip()) or "node"
    return os.path.join(get_node_session_dir(), f"{safe_node_id}.json")


def load_node_session(node_id: str) -> dict[str, Any]:
    data = read_json_file(get_node_session_path(node_id))
    return data if isinstance(data, dict) else {}


def save_node_session(node_id: str, data: dict[str, Any]) -> None:
    write_json_file(get_node_session_path(node_id), data)


def _deep_merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def merge_node_session(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    current = load_node_session(node_id)
    merged = _deep_merge_dict(current, patch)
    save_node_session(node_id, merged)
    return merged


def extract_json_array_from_markdown(raw_output: str) -> list[dict[str, Any]] | None:
    if not raw_output:
        return None

    fenced = re.search(r"```json\s*(.*?)\s*```", raw_output, re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else raw_output
    try:
        data = json.loads(candidate)
        if isinstance(data, list):
            return data
    except Exception:
        pass

    span = re.search(r"(\[\s*{[\s\S]*}\s*\])", raw_output)
    if not span:
        return None

    try:
        data = json.loads(span.group(1))
        return data if isinstance(data, list) else None
    except Exception:
        return None


def build_dependency_context(node_id: str) -> str:
    req = _store().get_requirement(node_id)
    if not req:
        return "No dependency information available."

    deps = req.get("dependencies", [])
    if not deps:
        return "No dependencies for this node. This is a root/independent feature."

    lines = [
        "### Dependency Context (Previously Implemented Modules)",
        (
            "IMPORTANT: You MUST reuse and import the following existing interfaces if your "
            "current feature relies on them, instead of reinventing them. If you reuse them, "
            "set `reuse: true` in your JSON output."
        ),
        "",
    ]

    for dep_id in deps:
        dep_req = _store().get_requirement(dep_id)
        if not dep_req:
            continue

        lines.append(f"#### Dependency Requirement Node: [{dep_id}]")
        lines.append(f"Description: {dep_req.get('description', 'N/A')}")

        dep_ifaces = _store().list_interfaces(req_id=dep_id)
        if dep_ifaces:
            lines.append("Available Interfaces from this Dependency:")
            for iface in dep_ifaces:
                lines.append(f"  - ID: `{iface.get('interface_id')}` (Type: {iface.get('type')})")
                if iface.get("file_path"):
                    lines.append(f"    File Path: `{iface.get('file_path')}`")
                if iface.get("first_line"):
                    lines.append(f"    Signature: `{iface.get('first_line')}`")
                try:
                    content = json.loads(iface.get("content", "{}"))
                except Exception:
                    content = {}
                if content.get("description"):
                    lines.append(f"    Description: {content['description']}")
                if content.get("inputs"):
                    lines.append(f"    Inputs: {content['inputs']}")
                if content.get("outputs"):
                    lines.append(f"    Outputs: {content['outputs']}")
                if content.get("callers"):
                    lines.append(f"    Callers: {content['callers']}")
                if content.get("callees"):
                    lines.append(f"    Callees: {content['callees']}")
        lines.append("")

    return "\n".join(lines)


async def check_prerequisites(
    app_type: str,
    log_cb: Callable[[str, str], Awaitable[None]],
) -> bool:
    if app_type == "android":
        try:
            process = await asyncio.create_subprocess_shell(
                "java -version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
            version_output = stderr.decode() if stderr else stdout.decode()
            if process.returncode != 0:
                await log_cb(
                    "System",
                    "Prerequisite check FAILED: Java is not installed or not on PATH. Android builds require JDK 17+.",
                )
                return False
            first_line = version_output.strip().split("\n")[0] if version_output.strip() else "unknown"
            await log_cb("System", f"Prerequisite check passed: Java found ({first_line})")
        except Exception as exc:
            await log_cb(
                "System",
                f"Prerequisite check FAILED: Could not verify Java installation: {str(exc)}",
            )
            return False

        sdk_root = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
        if sdk_root and os.path.isdir(sdk_root):
            await log_cb("System", f"Prerequisite check passed: Android SDK found at {sdk_root}")
        else:
            default_paths = [
                "D:/Android/Sdk",
                os.path.expanduser("~/AppData/Local/Android/Sdk"),
                os.path.expanduser("~/Android/Sdk"),
                "/usr/local/android-sdk",
                os.path.expanduser("~/Library/Android/sdk"),
            ]
            found = False
            for path in default_paths:
                if os.path.isdir(path):
                    sdk_root = path
                    os.environ["ANDROID_SDK_ROOT"] = path
                    await log_cb(
                        "System",
                        f"Prerequisite check passed: Android SDK found at {path} (auto-detected)",
                    )
                    found = True
                    break
            if not found:
                await log_cb(
                    "System",
                    "Prerequisite check FAILED: Android SDK not found. Set ANDROID_SDK_ROOT environment variable or install Android Studio.",
                )
                return False

        sdkmanager_path = os.path.join(sdk_root, "cmdline-tools", "latest", "bin", "sdkmanager")
        if os.name == "nt":
            sdkmanager_path += ".bat"
        if os.path.exists(sdkmanager_path):
            await log_cb("System", "Auto-accepting Android SDK licenses...")
            try:
                accept_process = await asyncio.create_subprocess_shell(
                    f'yes | "{sdkmanager_path}" --licenses',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(accept_process.communicate(), timeout=30)
                await log_cb("System", "Android SDK licenses accepted.")
            except Exception as exc:
                await log_cb("System", f"SDK license acceptance skipped (non-fatal): {str(exc)}")
        else:
            await log_cb("System", "sdkmanager not found at expected path; skipping license acceptance.")
        return True

    if app_type == "web":
        try:
            process = await asyncio.create_subprocess_shell(
                "node --version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
            if process.returncode == 0:
                version = stdout.decode().strip()
                await log_cb("System", f"Prerequisite check passed: Node.js found ({version})")
                return True
            await log_cb(
                "System",
                "Prerequisite check FAILED: Node.js is not installed or not on PATH. Web builds require Node.js LTS.",
            )
            return False
        except Exception as exc:
            await log_cb(
                "System",
                f"Prerequisite check FAILED: Could not verify Node.js installation: {str(exc)}",
            )
            return False

    return True


def extract_modified_files_from_messages(messages: list[dict[str, Any]]) -> list[str]:
    modified = set()
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tool_call in msg["tool_calls"]:
                func = (
                    tool_call.get("function", {})
                    if isinstance(tool_call, dict)
                    else (tool_call.function if hasattr(tool_call, "function") else {})
                )
                tool_name = (
                    func.get("name")
                    if isinstance(func, dict)
                    else (func.name if hasattr(func, "name") else "")
                )
                if tool_name in {"write_file", "edit_file"}:
                    try:
                        args_raw = (
                            func.get("arguments", "{}")
                            if isinstance(func, dict)
                            else (func.arguments if hasattr(func, "arguments") else "{}")
                        )
                        args = json.loads(args_raw)
                    except Exception:
                        args = {}
                    file_path = args.get("path", "") or args.get("file_path", "")
                    if file_path:
                        modified.add(file_path)
    return list(modified)

# ======================================================================================
#                                   CLI Logging
# ======================================================================================

class Spinner:
    FRAMES = ["|", "/", "-", "\\"]
    THINKING_FRAMES = [".  ", ".. ", "...", " ..", "  .", "   "]
    THINKING_COLORS = [Fore.BLUE, Fore.GREEN, Fore.YELLOW, Fore.GREEN]
    WORK_WORDS = ["Working", "Shaping", "Running", "Checking"]
    WORK_COLORS = [Fore.CYAN, Fore.BLUE, Fore.CYAN, Fore.MAGENTA]
    THINKING_INTERVAL = 0.25
    WORK_INTERVAL = 0.25

    def __init__(self):
        self._active = False
        self._thread = None
        self._text = ""
        self._mode = "work"
        self._last_render_width = 0

    def start(self, text: str = "Thinking", mode: str = "work"):
        if self._active:
            self._text = text
            self._mode = mode
            return
        self._active = True
        self._text = text
        self._mode = mode
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self):
        self._active = False
        if self._thread:
            self._thread.join(timeout=0.5)
        clear_width = max(self._last_render_width, 120)
        sys.stdout.write("\r" + " " * clear_width + "\r")
        sys.stdout.flush()
        self._last_render_width = 0

    def _spin(self):
        idx = 0
        while self._active:
            if self._mode == "thinking":
                dots = self.THINKING_FRAMES[idx % len(self.THINKING_FRAMES)]
                color = self.THINKING_COLORS[idx % len(self.THINKING_COLORS)]
                if self._text:
                    line = f"\r  {color}Thinking{dots}{Style.RESET_ALL} {Fore.WHITE}{self._text}{Style.RESET_ALL}"
                else:
                    line = f"\r  {color}Thinking{dots}{Style.RESET_ALL}"
            else:
                word = self.WORK_WORDS[idx % len(self.WORK_WORDS)]
                color = self.WORK_COLORS[idx % len(self.WORK_COLORS)]
                dots = self.THINKING_FRAMES[idx % len(self.THINKING_FRAMES)]
                if self._text:
                    line = f"\r  {color}{word}{dots}{Style.RESET_ALL} {Fore.WHITE}{self._text}{Style.RESET_ALL}"
                else:
                    line = f"\r  {color}{word}{dots}{Style.RESET_ALL}"
            plain_line = re.sub(r"\x1b\[[0-9;]*m", "", line)
            self._last_render_width = max(self._last_render_width, len(plain_line))
            sys.stdout.write(line)
            sys.stdout.flush()
            idx += 1
            threading.Event().wait(
                self.THINKING_INTERVAL if self._mode == "thinking" else self.WORK_INTERVAL
            )


class DebugLogger:
    def __init__(self, log_path: str, reset_existing: bool = True):
        self._path = log_path
        self._lock = threading.Lock()
        self._ensure_log_file(reset=reset_existing)

    def _ensure_log_file(self, reset: bool = False):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        if reset or not os.path.exists(self._path):
            with open(self._path, "w", encoding="utf-8") as file:
                file.write(f"=== ARC Debug Log | {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n\n")

    def log(self, tag: str, content: str):
        timestamp = time.strftime("%H:%M:%S")
        with self._lock:
            self._ensure_log_file()
            with open(self._path, "a", encoding="utf-8") as file:
                file.write(f"[{timestamp}] [{tag}] {content}\n")


class PromptDumpLogger:
    def __init__(self, dump_dir: str, reset_existing: bool = True):
        self._dir = dump_dir
        self._lock = threading.Lock()
        self._counter = 0
        self._ensure_dir(reset=reset_existing)

    def _ensure_dir(self, reset: bool = False):
        if reset and os.path.isdir(self._dir):
            shutil.rmtree(self._dir, ignore_errors=True)
        os.makedirs(self._dir, exist_ok=True)

    @staticmethod
    def _safe_segment(value: str, fallback: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
        return cleaned or fallback

    def dump(self, agent_name: str, node_id: str | None, step: int, payload: dict[str, Any]) -> str:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        safe_agent = self._safe_segment(agent_name, "Agent")
        safe_node = self._safe_segment(node_id or "GLOBAL", "GLOBAL")
        with self._lock:
            self._counter += 1
            sequence = self._counter
            filename = f"{timestamp}-{sequence:04d}-{safe_agent}-{safe_node}-step{int(step):02d}.md"
            path = os.path.join(self._dir, filename)
            request = payload.get("request", {}) if isinstance(payload, dict) else {}
            messages = request.get("messages", []) if isinstance(request, dict) else []
            tools = request.get("tools", []) if isinstance(request, dict) else []
            lines = [
                "# Model Request Dump",
                "",
                f"- Timestamp: `{timestamp}`",
                f"- Agent: `{agent_name}`",
                f"- Node: `{node_id or 'GLOBAL'}`",
                f"- Step: `{int(step)}`",
                f"- Model: `{request.get('model', '')}`",
                f"- Temperature: `{request.get('temperature', '')}`",
                f"- Tool Choice: `{request.get('tool_choice', '')}`",
                "",
                "## Messages",
            ]
            tool_call_map = {}
            for index, message in enumerate(messages, start=1):
                role = str(message.get("role", "unknown"))
                lines.append("")
                lines.append(f"### {index}. `{role}`")
                if role == "tool":
                    tool_call_id = str(message.get("tool_call_id", "") or "")
                    tool_name = str(message.get("name", "") or "")
                    if tool_call_id:
                        lines.append(f"- Tool Call ID: `{tool_call_id}`")
                    if tool_name:
                        lines.append(f"- Tool Name: `{tool_name}`")
                    linked_call = tool_call_map.get(tool_call_id)
                    if linked_call:
                        linked_name = str(linked_call.get("name", "") or "")
                        linked_args = linked_call.get("arguments", "")
                        if linked_name and linked_name != tool_name:
                            lines.append(f"- Linked Tool Use Name: `{linked_name}`")
                        lines.append("- Linked Tool Use Arguments:")
                        if isinstance(linked_args, str):
                            lines.append("```json")
                            lines.append(linked_args)
                            lines.append("```")
                        else:
                            lines.append("```json")
                            lines.append(json.dumps(linked_args, indent=2, ensure_ascii=False))
                            lines.append("```")
                content = message.get("content", "")
                if isinstance(content, str):
                    lines.append("```text")
                    lines.append(content)
                    lines.append("```")
                else:
                    lines.append("```json")
                    lines.append(json.dumps(content, indent=2, ensure_ascii=False))
                    lines.append("```")

                tool_calls = message.get("tool_calls", [])
                if tool_calls:
                    lines.append("")
                    lines.append("#### tool_calls")
                    for call_index, tool_call in enumerate(tool_calls, start=1):
                        if not isinstance(tool_call, dict):
                            continue
                        tool_call_id = str(tool_call.get("id", "") or "")
                        function_payload = tool_call.get("function", {}) or {}
                        tool_name = str(function_payload.get("name", "") or "")
                        tool_args = function_payload.get("arguments", "")
                        if tool_call_id:
                            tool_call_map[tool_call_id] = {
                                "name": tool_name,
                                "arguments": tool_args,
                            }
                        lines.append("")
                        lines.append(f"- Tool Call {call_index}: `{tool_name or 'unknown'}`")
                        if tool_call_id:
                            lines.append(f"  ID: `{tool_call_id}`")
                        if isinstance(tool_args, str):
                            lines.append("```json")
                            lines.append(tool_args)
                            lines.append("```")
                        else:
                            lines.append("```json")
                            lines.append(json.dumps(tool_args, indent=2, ensure_ascii=False))
                            lines.append("```")

            # lines.extend(["", "## Tools", "```json", json.dumps(tools, indent=2, ensure_ascii=False), "```"])
            # lines.extend(["", "## Full Request JSON", "```json", json.dumps(payload, indent=2, ensure_ascii=False), "```"])
            with open(path, "w", encoding="utf-8") as file:
                file.write("\n".join(lines))
        return path


_spinner = Spinner()
debug_logger: Optional[DebugLogger] = None
prompt_dump_logger: Optional[PromptDumpLogger] = None
ARC_DEBUG_ENABLED = str(os.environ.get("ARC_DEBUG", "0")).strip().lower() not in {"0", "false", "no", "off", ""}

_AGENT_COLORS = {
    "System": Fore.WHITE,
    "RequirementLoader": Fore.YELLOW,
    "DependencyManager": Fore.YELLOW,
    "Compiler": Fore.YELLOW,
    "InterfaceDesigner": Fore.MAGENTA,
    "TestGenerator": Fore.GREEN,
    "TestDrivenDeveloper": Fore.CYAN,
}

_STATUS_COLORS = {
    "analyzing": Fore.YELLOW,
    "designed": Fore.MAGENTA,
    "completed": Fore.GREEN,
    "error": Fore.RED,
    "warning": Fore.YELLOW,
    "info": Fore.CYAN,
}


class CliProgressView:
    PHASE_LABELS = {
        "DESIGN": "Design",
        "IMPLEMENT": "Implement",
    }

    TOOL_ACTIONS = {
        "read_file": "Inspecting files",
        "list_directory": "Scanning workspace",
        "glob": "Finding targets",
        "grep": "Searching code",
        "search_interfaces_by_keyword": "Checking reuse",
        "search_interfaces_by_relation": "Checking relations",
        "find_interface_impacts": "Checking impacts",
        "get_node_relations": "Checking graph",
        "write_file": "Writing code",
        "edit_file": "Editing code",
        "delete_file": "Cleaning files",
        "execute_command": "Running command",
        "run_build": "Verifying build",
        "run_tests": "Running tests",
    }

    TOOL_LABELS = {
        "read_file": "open file",
        "write_file": "write file",
        "edit_file": "patch file",
        "delete_file": "remove file",
        "list_directory": "scan folder",
        "glob": "find files",
        "grep": "search code",
        "search_interfaces_by_keyword": "search reuse",
        "search_interfaces_by_relation": "trace relations",
        "find_interface_impacts": "trace impacts",
        "get_node_relations": "inspect graph",
        "execute_command": "run command",
        "run_build": "run build",
        "run_tests": "run tests",
    }

    def __init__(self) -> None:
        self.current_node_id: str | None = None
        self.current_phase: str | None = None
        self.current_agent: str | None = None
        self._last_line: str | None = None
        self._pending_spinner: tuple[str, str] | None = None
        self._next_render_mode: str | None = None
        self._tool_batch_open = False
        self._tool_batch_lines: list[str] = []
        self._transient_line_count = 0

    @staticmethod
    def _plain_node(node_id: str | None) -> str:
        return str(node_id or "ARC").strip() or "ARC"

    @staticmethod
    def _pretty_name(value: str | None) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", text)
        if not parts:
            return text
        return " ".join(part if part.isupper() else part.capitalize() for part in parts)

    @staticmethod
    def _compact_path(path: str) -> str:
        normalized = str(path or "").replace("\\", "/").strip().rstrip("/")
        if not normalized:
            return ""
        parts = [part for part in normalized.split("/") if part]
        if not parts:
            return ""
        if os.path.isabs(normalized):
            return "/".join(parts[-4:])
        if len(normalized) <= 56:
            return normalized
        return "/".join(parts[-4:])

    @staticmethod
    def _clip_text(value: str, limit: int = 48) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def _emit_once(self, line: str | None) -> str | None:
        if not line:
            return None
        if line == self._last_line:
            return None
        self._last_line = line
        return line

    @staticmethod
    def _muted(text: str) -> str:
        return f"{Fore.LIGHTBLACK_EX}{text}{Style.RESET_ALL}"

    def resume_pending_spinner(self) -> None:
        if not self._pending_spinner:
            return
        text, mode = self._pending_spinner
        self._pending_spinner = None
        _spinner.start(text, mode=mode)

    def _queue_spinner(self, text: str, mode: str) -> None:
        self._pending_spinner = (text, mode)

    def _clear_transient_block(self) -> None:
        if self._transient_line_count <= 0:
            return
        for _ in range(self._transient_line_count):
            sys.stdout.write("\x1b[F\x1b[2K")
        sys.stdout.flush()
        self._transient_line_count = 0

    def _redraw_tool_batch(self) -> None:
        self._clear_transient_block()
        if not self._tool_batch_lines:
            return
        block = "\n".join(self._tool_batch_lines)
        sys.stdout.write(block + "\n")
        sys.stdout.flush()
        self._transient_line_count = len(block.splitlines())

    def emit_rendered(self, rendered: str) -> None:
        mode = self._next_render_mode or "persistent"
        if mode == "transient":
            if not self._tool_batch_open:
                self._clear_transient_block()
                self._tool_batch_lines = []
                self._tool_batch_open = True
            self._tool_batch_lines.append(rendered)
            self._redraw_tool_batch()
            return

        self._tool_batch_open = False
        self._tool_batch_lines = []
        self._clear_transient_block()
        print(rendered)

    def _build_spinner_label(self, agent: str, node_id: str | None, suffix: str | None = None) -> str:
        label_parts: list[str] = []
        effective_node = node_id or self.current_node_id
        if effective_node:
            label_parts.append(self._plain_node(effective_node))
        if self.current_phase:
            label_parts.append(self.PHASE_LABELS.get(self.current_phase, self.current_phase.title()))
        if agent not in {"System", "Compiler", "RequirementLoader"}:
            label_parts.append(self._pretty_name(agent))
        if suffix:
            label_parts.append(suffix)
        return " | ".join(label_parts)

    @staticmethod
    def _parse_tool_payload(message: str) -> tuple[str, dict[str, Any]]:
        raw_payload = message.split("Calling tool:", 1)[1].strip()
        try:
            payload = json.loads(raw_payload)
        except Exception:
            tool_match = re.search(r"`(\w+)`", message)
            tool_name = tool_match.group(1) if tool_match else "tool"
            args_match = re.search(r"with args:\s*(.*)", message, re.DOTALL)
            if not args_match:
                return tool_name, {}
            try:
                return tool_name, json.loads(args_match.group(1).strip())
            except Exception:
                return tool_name, {}
        tool_name = str(payload.get("tool") or "tool").strip() or "tool"
        args = payload.get("args")
        return tool_name, args if isinstance(args, dict) else {}

    def _summarize_tool_args(self, tool_name: str, args: dict[str, Any]) -> str:
        if not isinstance(args, dict):
            return ""

        path = self._compact_path(
            str(args.get("path") or args.get("file_path") or args.get("target") or "").strip()
        )
        if tool_name == "read_file" and path:
            offset = args.get("offset")
            limit = args.get("limit")
            if isinstance(offset, int) and isinstance(limit, int) and limit > 0:
                return f"{path}:{offset}-{offset + limit - 1}"
            if isinstance(offset, int) and offset > 0:
                return f"{path}:{offset}+"
            return path
        if tool_name == "write_file" and path:
            content = str(args.get("content") or "")
            line_count = len(content.splitlines()) if content else 0
            if line_count:
                return f"{path} ({line_count} lines)"
            return path
        if tool_name == "edit_file" and path:
            replace_all = bool(args.get("replace_all"))
            old_string = str(args.get("old_string") or "")
            snippet = self._clip_text(" ".join(old_string.split()), 36)
            mode = "replace all" if replace_all else "exact block"
            if snippet:
                return f"{path} [{mode}] \"{snippet}\""
            return f"{path} [{mode}]"
        if tool_name == "delete_file" and path:
            return path
        if tool_name == "list_directory":
            depth = args.get("depth")
            if path and isinstance(depth, int):
                return f"{path}/ depth {depth}"
            if path:
                return path
        if tool_name == "glob":
            pattern = self._clip_text(str(args.get("pattern") or "").strip(), 36)
            search_root = self._compact_path(str(args.get("path") or ".").strip()) or "."
            if pattern:
                return f"\"{pattern}\" in {search_root}/"
        if tool_name == "grep":
            pattern = self._clip_text(str(args.get("pattern") or "").strip(), 36)
            search_root = self._compact_path(str(args.get("path") or ".").strip()) or "."
            glob_text = self._clip_text(str(args.get("glob") or "").strip(), 24)
            output_mode = str(args.get("output_mode") or "files_with_matches").strip()
            mode_label = {
                "files_with_matches": "files",
                "count": "counts",
                "content": "content",
            }.get(output_mode, output_mode)
            detail = f"\"{pattern}\" in {search_root}/" if pattern else search_root
            if glob_text:
                detail += f" ({glob_text})"
            return f"{detail} [{mode_label}]"
        if tool_name == "run_tests":
            test_files = args.get("test_files")
            if isinstance(test_files, list) and test_files:
                if len(test_files) == 1:
                    return self._compact_path(str(test_files[0]))
                return f"{len(test_files)} target file(s)"
            test_type = str(args.get("test_type") or "").strip()
            if test_type:
                return f"{test_type} batch"
        if tool_name == "run_build":
            return "project build"
        if tool_name == "execute_command":
            command = self._clip_text(str(args.get("command") or "").strip(), 42)
            cwd = self._compact_path(str(args.get("cwd") or ".").strip()) or "."
            if command:
                return f"\"{command}\" in {cwd}/"
        if tool_name == "search_interfaces_by_keyword":
            query = self._clip_text(str(args.get("keyword") or "").strip(), 32)
            limit = args.get("limit")
            if query and isinstance(limit, int):
                return f"for \"{query}\" (top {limit})"
            if query:
                return f"for \"{query}\""
        if tool_name == "search_interfaces_by_relation":
            node = str(args.get("node_id") or "").strip()
            relation = str(args.get("relation_type") or "all").strip()
            if node:
                return f"around {node} [{relation}]"
        if tool_name == "find_interface_impacts":
            interface_id = self._clip_text(str(args.get("interface_id") or "").strip(), 36)
            if interface_id:
                return f"for {interface_id}"
        if tool_name == "get_node_relations":
            node = str(args.get("node_id") or "").strip()
            if node:
                return f"for {node}"
        return ""

    def _render_tool_call_line(self, agent: str, message: str, node_id: str | None) -> str | None:
        tool_name, tool_args = self._parse_tool_payload(message)
        activity = self.TOOL_ACTIONS.get(tool_name, "Using tool")
        label = self.TOOL_LABELS.get(tool_name, tool_name.replace("_", " "))
        detail = self._summarize_tool_args(tool_name, tool_args)
        spinner_label = self._build_spinner_label(agent=agent, node_id=node_id, suffix=activity)
        self._queue_spinner(spinner_label, "work")

        node_label = self._plain_node(node_id or self.current_node_id)
        agent_label = self._pretty_name(agent) or agent
        line = f"{node_label:<10}  {agent_label:<18} {label:<16}"
        if detail:
            line += f" {detail}"
        return self._emit_once(self._muted(line))

    def render(self, agent: str, message: str, status: str | None = None, node_id: str | None = None) -> str | None:
        self._next_render_mode = None
        if message.startswith("Thinking..."):
            self._tool_batch_open = False
            self._tool_batch_lines = []
            self._start_thinking(agent=agent, node_id=node_id)
            return None

        if message.startswith("Calling tool:"):
            _spinner.stop()
            self._next_render_mode = "transient"
            return self._render_tool_call_line(agent=agent, message=message, node_id=node_id)

        if message.startswith("Tool result:"):
            _spinner.stop()
            self._next_render_mode = "transient"
            return self._format_progress_line_v2(agent=agent, message=message, status=status, node_id=node_id)

        if message == "Task completed.":
            _spinner.stop()
            self._tool_batch_open = False
            self._tool_batch_lines = []
            return None

        _spinner.stop()
        self._tool_batch_open = False
        self._tool_batch_lines = []
        self._next_render_mode = "persistent"
        return self._format_progress_line_v2(agent=agent, message=message, status=status, node_id=node_id)

    def _start_thinking(self, agent: str, node_id: str | None) -> None:
        _spinner.start(self._build_spinner_label(agent=agent, node_id=node_id), mode="thinking")

    def _start_spinner(self, agent: str, node_id: str | None, activity: str) -> None:
        _spinner.start(self._build_spinner_label(agent=agent, node_id=node_id, suffix=activity), mode="work")

    @staticmethod
    def _extract_tool_name(message: str) -> str:
        tool_match = re.search(r"`(\w+)`", message)
        return tool_match.group(1) if tool_match else ""

    @staticmethod
    def _summarize_paths(message: str, prefix: str, max_items: int = 4) -> str:
        raw = message.split(prefix, 1)[1].strip()
        items = [item.strip() for item in raw.split(",") if item.strip()]
        if not items:
            return ""
        head = ", ".join(items[:max_items])
        if len(items) > max_items:
            head += f", +{len(items) - max_items} more"
        return head

    def _render_artifact_panel(self, active_node: str | None, payload: dict[str, Any]) -> str | None:
        kind = str(payload.get("kind") or "").strip()
        total = int(payload.get("total") or 0)
        type_counts = payload.get("type_counts") or {}
        files = payload.get("files") or []
        items = payload.get("items") or []

        if kind not in {"design", "tests"}:
            return None

        node_label = self._plain_node(active_node)
        title = "Design Summary" if kind == "design" else "Test Summary"
        accent = Fore.MAGENTA if kind == "design" else Fore.GREEN
        icon = "Interfaces" if kind == "design" else "Tests"

        count_bits = []
        preferred_order = ["UI", "API", "FUNC", "DB"] if kind == "design" else ["Unit", "Integration", "E2E"]
        for key in preferred_order:
            value = type_counts.get(key)
            if value:
                count_bits.append(f"{key} {value}")
        for key, value in type_counts.items():
            if key not in preferred_order and value:
                count_bits.append(f"{key} {value}")
        count_line = "  ".join(count_bits) if count_bits else "No typed artifacts"

        top_files = [str(item).strip().replace("\\", "/") for item in files if str(item).strip()]
        file_line = ", ".join(top_files)
        if not file_line:
            file_line = "No file materialization"

        inner_width = 104
        def wrap_panel_line(label: str, value: str) -> list[str]:
            text = str(value or "").strip() or "-"
            available = inner_width - 12
            chunks = [text[i:i + available] for i in range(0, len(text), available)] or ["-"]
            lines: list[str] = []
            for idx, chunk in enumerate(chunks):
                label_text = label if idx == 0 else ""
                lines.append(
                    f"{accent}|{Style.RESET_ALL} {Fore.LIGHTBLACK_EX}{label_text:<10}{Style.RESET_ALL} {Fore.WHITE}{chunk:<{available}}{Style.RESET_ALL}"
                )
            return lines

        lines = [
            "",
            f"{accent}+{'-' * inner_width}{Style.RESET_ALL}",
            f"{accent}|{Style.RESET_ALL} {Fore.CYAN}{node_label:<10}{Style.RESET_ALL} {Fore.WHITE}{title:<91}{Style.RESET_ALL}",
            f"{accent}|{'-' * inner_width}{Style.RESET_ALL}",
            f"{accent}|{Style.RESET_ALL} {Fore.LIGHTBLACK_EX}{icon:<10}{Style.RESET_ALL} {Fore.WHITE}{f'{total} total':<92}{Style.RESET_ALL}",
            f"{accent}|{Style.RESET_ALL} {Fore.LIGHTBLACK_EX}Breakdown {Style.RESET_ALL} {Fore.WHITE}{count_line:<92}{Style.RESET_ALL}",
            f"{accent}|{'-' * inner_width}{Style.RESET_ALL}",
        ]
        lines.extend(wrap_panel_line("Files", file_line))
        lines.append(f"{accent}|{'-' * inner_width}{Style.RESET_ALL}")

        for item in items:
            if kind == "design":
                item_type = str(item.get("type") or "?").strip()
                interface_id = str(item.get("id") or "").strip() or "unknown-interface"
                path = str(item.get("path") or "").strip().replace("\\", "/") or "-"
                reuse = "reuse" if item.get("reuse") else "new"
                detail = f"{item_type:<4} {interface_id}  {path}  {reuse}"
            else:
                item_type = str(item.get("type") or "?").strip()
                test_id = str(item.get("id") or "").strip() or "unknown-test"
                path = str(item.get("path") or "").strip().replace("\\", "/") or "-"
                interfaces = item.get("interfaces") or []
                coverage = ", ".join(str(x).strip() for x in interfaces if str(x).strip()) or "contract"
                detail = f"{item_type:<11} {test_id}  {path}  {coverage}"
            lines.extend(wrap_panel_line(">", detail))

        return "\n".join(lines) + "\n\n"

    def _format_progress_line_v2(
        self,
        agent: str,
        message: str,
        status: str | None = None,
        node_id: str | None = None,
    ) -> str | None:
        active_node = node_id or self.current_node_id
        if agent not in {"System", "Compiler", "RequirementLoader"}:
            self.current_agent = agent
        node_prefix = f"{Fore.CYAN}{self._plain_node(active_node):<10}{Style.RESET_ALL}"

        def stage_line(stage: str, detail: str, tone: str = "info") -> str:
            palette = {
                "info": Fore.CYAN,
                "ok": Fore.GREEN,
                "warn": Fore.YELLOW,
                "fail": Fore.RED,
                "agent": Fore.MAGENTA,
                "test": Fore.GREEN,
            }
            color = palette.get(tone, Fore.WHITE)
            return f"{node_prefix}  {color}{stage:<12}{Style.RESET_ALL} {Fore.WHITE}{detail}{Style.RESET_ALL}"

        def header_line(title: str, detail: str) -> str:
            bar = f"{Fore.BLUE}{'=' * 76}{Style.RESET_ALL}"
            return f"\n{bar}\n{Fore.CYAN}{title}{Style.RESET_ALL}  {Fore.WHITE}{detail}{Style.RESET_ALL}"

        run_match = re.match(r"Running (DESIGN|IMPLEMENT) for node ([^.]+)\.\.\.", message)
        if agent == "Compiler" and run_match:
            phase = run_match.group(1)
            self.current_phase = phase
            self.current_node_id = run_match.group(2).strip()
            self.current_agent = None
            return self._emit_once(
                header_line(
                    f"{self._plain_node(self.current_node_id)} / {self.PHASE_LABELS.get(phase, phase.title())}",
                    "starting",
                )
            )

        done_match = re.match(r"(DESIGN|IMPLEMENT) completed for node ([^.]+)\.", message)
        if agent == "Compiler" and done_match:
            phase = done_match.group(1)
            node = done_match.group(2).strip()
            return self._emit_once(
                f"{Fore.GREEN}  done{Style.RESET_ALL}      {Fore.CYAN}{node:<10}{Style.RESET_ALL} {Fore.WHITE}{self.PHASE_LABELS.get(phase, phase.title())} complete{Style.RESET_ALL}"
            )

        if agent == "Compiler" and message == "ARC compilation started.":
            return self._emit_once(header_line("ARC", "compilation started"))
        if message.startswith("Tool result:"):
            summary = message.split("Tool result:", 1)[1].strip()
            node_label = self._plain_node(active_node)
            agent_label = self._pretty_name(agent) or agent
            line = f"{node_label:<10}  {agent_label:<18} {Fore.LIGHTBLACK_EX}{summary}{Style.RESET_ALL}"
            return self._emit_once(line)
        if agent == "Compiler" and message.startswith("Persisting requirement tree and preparing processing queue"):
            return self._emit_once(stage_line("Prepare", "Loading requirement graph"))
        if agent == "Compiler" and message.startswith("Loaded processing queue with"):
            queue_match = re.search(r"with (\d+) task\(s\)", message)
            task_count = queue_match.group(1) if queue_match else "?"
            return self._emit_once(stage_line("Queue", f"{task_count} task(s) scheduled"))
        if agent == "Compiler" and message.startswith("Existing processing queue detected"):
            return self._emit_once(stage_line("Resume", "Continuing from saved queue"))
        if agent == "Compiler" and message.startswith("Compilation finished successfully"):
            return self._emit_once(f"\n{Fore.GREEN}SUCCESS{Style.RESET_ALL}  {Fore.WHITE}Compilation finished successfully{Style.RESET_ALL}")
        if agent == "Compiler" and message.startswith("Compilation finished with"):
            return self._emit_once(f"\n{Fore.RED}FAILED{Style.RESET_ALL}   {Fore.WHITE}{message}{Style.RESET_ALL}")
        if agent == "Compiler" and message.startswith("Running git checkpoint for"):
            return self._emit_once(stage_line("Checkpoint", "Saving milestone"))
        if agent == "Compiler" and message == "No file changes detected for this checkpoint.":
            return None
        if agent == "Compiler" and " group passed from the latest run_tests result." in message:
            test_type = message.split(" group passed", 1)[0].strip()
            return self._emit_once(stage_line("Tests", f"{test_type} batch passed", tone="ok"))
        if agent == "Compiler" and " group did not pass from the latest run_tests result." in message:
            test_type = message.split(" group did not pass", 1)[0].strip()
            return self._emit_once(stage_line("Tests", f"{test_type} batch still failing", tone="fail"))
        if agent == "Compiler" and message.startswith("Test summary:"):
            return self._emit_once(stage_line("Summary", message.split(":", 1)[1].strip(), tone="test"))
        if agent == "Compiler" and message == "All generated tests passed.":
            return self._emit_once(stage_line("Result", "All generated tests passed", tone="ok"))
        if agent == "Compiler" and message == "Some generated tests are still failing.":
            return self._emit_once(stage_line("Result", "Some generated tests are still failing", tone="fail"))

        if agent == "System" and message.startswith("Initializing project environment"):
            return self._emit_once(stage_line("Workspace", "Preparing runtime workspace"))
        if agent == "System" and message.startswith("Initializing traceability database at"):
            return self._emit_once(stage_line("Workspace", "Preparing traceability store"))
        if agent == "System" and message.startswith("Reusing existing traceability database at"):
            return self._emit_once(stage_line("Resume", "Reusing traceability store"))
        if agent == "System" and message.startswith("Using app_type="):
            return None
        if agent == "System" and message.startswith("Copying template from"):
            return self._emit_once(stage_line("Workspace", "Scaffolding project template"))
        if agent == "System" and message == "Template files copied successfully.":
            return self._emit_once(stage_line("Workspace", "Template ready", tone="ok"))
        if agent == "System" and message.startswith("Configured web template"):
            return self._emit_once(stage_line("Workspace", "Web stack configured", tone="ok"))
        if agent == "System" and message.startswith("Installing backend dependencies"):
            return self._emit_once(stage_line("Deps", "Installing backend packages"))
        if agent == "System" and message.startswith("Installing frontend dependencies"):
            return self._emit_once(stage_line("Deps", "Installing frontend packages"))
        if agent == "System" and message.startswith("NPM install success in"):
            target = os.path.basename(message.rsplit(" ", 1)[-1].replace("\\", "/").rstrip("/")) or "target"
            return self._emit_once(stage_line("Deps", f"{target} packages ready", tone="ok"))
        if agent == "System" and message == "Full-stack workspace initialized completely.":
            return self._emit_once(stage_line("Workspace", "Initialization complete", tone="ok"))
        if agent == "System" and message == "Initializing Git repository...":
            return self._emit_once(stage_line("Checkpoint", "Initializing git history"))
        if "Prerequisite check passed" in message:
            return self._emit_once(stage_line("Check", message.replace("Prerequisite check passed: ", ""), tone="ok"))
        if "Prerequisite check FAILED" in message:
            return self._emit_once(stage_line("Check", message.replace("Prerequisite check FAILED: ", ""), tone="fail"))
        if agent == "System" and message.startswith("Analyzing visual element:"):
            return self._emit_once(stage_line("Visual", "Analyzing reference image"))
        if agent == "System" and message.startswith("Reusing cached visual analysis:"):
            return self._emit_once(stage_line("Visual", "Reusing cached reference analysis"))
        if agent == "System" and message.startswith("System test execution ("):
            test_match = re.match(r"System test execution \(([^)]+)\):\s*(.+)", message)
            if test_match:
                return self._emit_once(stage_line("Verify", f"{test_match.group(1)} :: {self._compact_path(test_match.group(2).strip())}"))
        if agent == "System" and message.startswith("Stored ") and "visual references for" in message:
            return self._emit_once(stage_line("Visual", "Reference analysis stored", tone="ok"))

        if agent == "RequirementLoader" and message.startswith("Reading requirements file:"):
            return self._emit_once(stage_line("Input", "Loading requirement model"))
        if agent == "InterfaceDesigner" and message.startswith("Running unified design session"):
            return self._emit_once(stage_line("Design", f"{self._pretty_name(agent)} is shaping interfaces and code", tone="agent"))
        if agent == "InterfaceDesigner" and message.startswith("Materialized files:"):
            files = self._summarize_paths(message, "Materialized files:")
            compact = ", ".join(self._compact_path(item.strip()) for item in files.split(",") if item.strip())
            return self._emit_once(stage_line("Artifacts", compact or "Code artifacts generated"))
        if agent == "InterfaceDesigner" and message.startswith("Artifact summary:"):
            payload_text = message.split("Artifact summary:", 1)[1].strip()
            try:
                payload = json.loads(payload_text)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                return self._emit_once(self._render_artifact_panel(active_node, payload))
        if agent == "InterfaceDesigner" and message.startswith("Stored ") and "interface definition" in message:
            count_match = re.search(r"Stored (\d+)", message)
            count = count_match.group(1) if count_match else "?"
            return self._emit_once(stage_line("Artifacts", f"{count} interface definition(s) recorded", tone="ok"))
        if agent == "InterfaceDesigner" and message.startswith("Skipping DESIGN for non-leaf node"):
            return self._emit_once(stage_line("Design", "Skipped: no scenarios or visual reference", tone="warn"))

        if agent == "TestGenerator" and message.startswith("Generating "):
            return self._emit_once(stage_line("Tests", f"{self._pretty_name(agent)} is generating validation plan", tone="test"))
        if agent == "TestGenerator" and message.startswith("Generated test files:"):
            files = self._summarize_paths(message, "Generated test files:")
            compact = ", ".join(self._compact_path(item.strip()) for item in files.split(",") if item.strip())
            return self._emit_once(stage_line("Artifacts", compact or "Test artifacts generated"))
        if agent == "TestGenerator" and message.startswith("Artifact summary:"):
            payload_text = message.split("Artifact summary:", 1)[1].strip()
            try:
                payload = json.loads(payload_text)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                return self._emit_once(self._render_artifact_panel(active_node, payload))
        if agent == "TestGenerator" and message.startswith("Stored ") and "test mapping item" in message:
            count_match = re.search(r"Stored (\d+)", message)
            count = count_match.group(1) if count_match else "?"
            return self._emit_once(stage_line("Tests", f"{count} test mapping item(s) recorded", tone="ok"))

        if agent == "TestDrivenDeveloper" and message.startswith("Implementing against "):
            batch_match = re.match(
                r"Implementing against ([A-Za-z0-9_]+) tests in (\d+) file\(s\) with budget (\d+)\.\.\.",
                message,
            )
            if batch_match:
                return self._emit_once(
                    stage_line("Implement", f"{batch_match.group(1)} batch on {batch_match.group(2)} file(s)", tone="agent")
                )
            return self._emit_once(stage_line("Implement", f"{self._pretty_name(agent)} is implementing", tone="agent"))
        if agent == "TestDrivenDeveloper" and message.startswith("Modified files after "):
            files = self._summarize_paths(message, ":")
            compact = ", ".join(self._compact_path(item.strip()) for item in files.split(",") if item.strip())
            return self._emit_once(stage_line("Artifacts", compact or "Files updated"))
        if agent == "TestDrivenDeveloper" and "ended without explicit IMPLEMENTED" in message:
            return self._emit_once(stage_line("Verify", "Implementation finished; validating through tests", tone="warn"))

        if status == "error":
            return self._emit_once(stage_line("Error", message, tone="fail"))
        if status == "warning":
            return self._emit_once(stage_line("Note", message, tone="warn"))
        if status == "info":
            return self._emit_once(stage_line("Info", message))
        if "skipping Test/TDD" in message:
            return self._emit_once(stage_line("Skip", message, tone="warn"))

        if agent in {"Compiler", "System", "RequirementLoader", "InterfaceDesigner", "TestGenerator", "TestDrivenDeveloper"}:
            return None
        return self._emit_once(stage_line("Info", message))

    def _format_progress_line(
        self,
        agent: str,
        message: str,
        status: str | None = None,
        node_id: str | None = None,
    ) -> str | None:
        active_node = node_id or self.current_node_id
        node_prefix = (
            f"{Fore.CYAN}[{active_node}]{Style.RESET_ALL} "
            if active_node
            else f"{Fore.CYAN}[ARC]{Style.RESET_ALL} "
        )

        run_match = re.match(r"Running (DESIGN|IMPLEMENT) for node ([^.]+)\.\.\.", message)
        if agent == "Compiler" and run_match:
            phase = run_match.group(1)
            self.current_phase = phase
            self.current_node_id = run_match.group(2).strip()
            phase_label = self.PHASE_LABELS.get(phase, phase.title())
            separator = f"{Fore.BLUE}{'-' * 64}{Style.RESET_ALL}"
            return (
                f"\n{separator}\n"
                f"{Fore.CYAN}[{self.current_node_id}]{Style.RESET_ALL} "
                f"{Fore.WHITE}{phase_label} phase started{Style.RESET_ALL}"
            )

        done_match = re.match(r"(DESIGN|IMPLEMENT) completed for node ([^.]+)\.", message)
        if agent == "Compiler" and done_match:
            phase = done_match.group(1)
            node = done_match.group(2).strip()
            return (
                f"{Fore.GREEN}[done]{Style.RESET_ALL} "
                f"{Fore.CYAN}[{node}]{Style.RESET_ALL} "
                f"{self.PHASE_LABELS.get(phase, phase.title())} phase completed"
            )

        if agent == "Compiler" and message == "ARC compilation started.":
            return f"{Fore.CYAN}[run]{Style.RESET_ALL} Compilation started"
        if agent == "Compiler" and message.startswith("Persisting requirement tree and preparing processing queue"):
            return f"{Fore.CYAN}[setup]{Style.RESET_ALL} Loading requirement graph and preparing the task queue"
        if agent == "Compiler" and message.startswith("Loaded processing queue with"):
            return f"{Fore.CYAN}[queue]{Style.RESET_ALL} {message}"
        if agent == "Compiler" and message.startswith("Existing processing queue detected"):
            return f"{Fore.CYAN}[resume]{Style.RESET_ALL} Resuming from the existing processing queue"
        if agent == "Compiler" and message.startswith("Compilation finished successfully"):
            return f"\n{Fore.GREEN}[success]{Style.RESET_ALL} Compilation finished successfully"
        if agent == "Compiler" and message.startswith("Compilation finished with"):
            return f"\n{Fore.RED}[failed]{Style.RESET_ALL} {message}"
        if agent == "Compiler" and message.startswith("Running git checkpoint for"):
            return f"{node_prefix}{Fore.BLUE}[git]{Style.RESET_ALL} Saving a checkpoint commit"
        if agent == "Compiler" and message == "No file changes detected for this checkpoint.":
            return f"{node_prefix}{Fore.BLUE}[git]{Style.RESET_ALL} No new file changes for this checkpoint"
        if agent == "Compiler" and " group passed from the latest run_tests result." in message:
            test_type = message.split(" group passed", 1)[0].strip()
            return f"{node_prefix}{Fore.GREEN}[pass]{Style.RESET_ALL} {test_type} tests passed"
        if agent == "Compiler" and " group did not pass from the latest run_tests result." in message:
            test_type = message.split(" group did not pass", 1)[0].strip()
            return f"{node_prefix}{Fore.RED}[fail]{Style.RESET_ALL} {test_type} tests still failing"
        if agent == "Compiler" and message.startswith("Test summary:"):
            return f"{node_prefix}{Fore.WHITE}[tests]{Style.RESET_ALL} {message.split(':', 1)[1].strip()}"
        if agent == "Compiler" and message == "All generated tests passed.":
            return f"{node_prefix}{Fore.GREEN}[tests]{Style.RESET_ALL} All generated tests passed"
        if agent == "Compiler" and message == "Some generated tests are still failing.":
            return f"{node_prefix}{Fore.RED}[tests]{Style.RESET_ALL} Some generated tests are still failing"

        if agent == "System" and message.startswith("Initializing project environment"):
            return f"{Fore.CYAN}[setup]{Style.RESET_ALL} Initializing workspace"
        if agent == "System" and message.startswith("Initializing traceability database at"):
            return f"{Fore.CYAN}[setup]{Style.RESET_ALL} Preparing traceability database"
        if agent == "System" and message.startswith("Reusing existing traceability database at"):
            return f"{Fore.CYAN}[resume]{Style.RESET_ALL} Reusing existing traceability database"
        if agent == "System" and message.startswith("Using app_type="):
            return None
        if agent == "System" and message.startswith("Copying template from"):
            return f"{Fore.CYAN}[setup]{Style.RESET_ALL} Copying project template"
        if agent == "System" and message == "Template files copied successfully.":
            return f"{Fore.GREEN}[ok]{Style.RESET_ALL} Template files copied"
        if agent == "System" and message.startswith("Configured web template"):
            return f"{Fore.GREEN}[ok]{Style.RESET_ALL} Web template configured"
        if agent == "System" and message.startswith("Installing backend dependencies"):
            return f"{Fore.CYAN}[deps]{Style.RESET_ALL} Installing backend dependencies"
        if agent == "System" and message.startswith("Installing frontend dependencies"):
            return f"{Fore.CYAN}[deps]{Style.RESET_ALL} Installing frontend dependencies"
        if agent == "System" and message.startswith("NPM install success in"):
            target = os.path.basename(message.rsplit(" ", 1)[-1].replace("\\", "/").rstrip("/")) or "target"
            return f"{Fore.GREEN}[ok]{Style.RESET_ALL} Installed dependencies for {target}"
        if agent == "System" and message == "Full-stack workspace initialized completely.":
            return f"{Fore.GREEN}[ok]{Style.RESET_ALL} Workspace initialized"
        if agent == "System" and message == "Initializing Git repository...":
            return f"{Fore.CYAN}[git]{Style.RESET_ALL} Initializing git repository"
        if "Prerequisite check passed" in message:
            return f"{Fore.GREEN}[ok]{Style.RESET_ALL} {message.replace('Prerequisite check passed: ', '')}"
        if "Prerequisite check FAILED" in message:
            return f"{Fore.RED}[fail]{Style.RESET_ALL} {message.replace('Prerequisite check FAILED: ', '')}"
        if agent == "System" and message.startswith("Analyzing visual element:"):
            return f"{node_prefix}{Fore.CYAN}[visual]{Style.RESET_ALL} Analyzing visual reference"
        if agent == "System" and message.startswith("Reusing cached visual analysis:"):
            return f"{node_prefix}{Fore.CYAN}[visual]{Style.RESET_ALL} Reusing cached visual analysis"
        if agent == "System" and message.startswith("System test execution ("):
            test_match = re.match(r"System test execution \(([^)]+)\):\s*(.+)", message)
            if test_match:
                return f"{node_prefix}{Fore.WHITE}[run]{Style.RESET_ALL} {test_match.group(1)} test · {test_match.group(2).strip()}"

        if agent == "RequirementLoader" and message.startswith("Reading requirements file:"):
            return f"{Fore.CYAN}[load]{Style.RESET_ALL} Reading requirements.yaml"
        if agent == "InterfaceDesigner" and message.startswith("Running unified design session"):
            return f"{node_prefix}{Fore.MAGENTA}[agent]{Style.RESET_ALL} InterfaceDesigner is designing interfaces and materializing owned code"
        if agent == "InterfaceDesigner" and message.startswith("Materialized files:"):
            return f"{node_prefix}{Fore.WHITE}[files]{Style.RESET_ALL} {self._summarize_paths(message, 'Materialized files:')}"
        if agent == "InterfaceDesigner" and message.startswith("Stored ") and "interface definition" in message:
            return f"{node_prefix}{Fore.MAGENTA}[artifacts]{Style.RESET_ALL} {message}"
        if agent == "InterfaceDesigner" and message.startswith("Skipping DESIGN for non-leaf node"):
            return f"{node_prefix}{Fore.YELLOW}[skip]{Style.RESET_ALL} Skipping non-leaf design without scenarios or visual reference"

        if agent == "TestGenerator" and message.startswith("Generating "):
            return f"{node_prefix}{Fore.GREEN}[agent]{Style.RESET_ALL} TestGenerator is building the test plan"
        if agent == "TestGenerator" and message.startswith("Generated test files:"):
            return f"{node_prefix}{Fore.WHITE}[files]{Style.RESET_ALL} {self._summarize_paths(message, 'Generated test files:')}"
        if agent == "TestGenerator" and message.startswith("Stored ") and "test mapping item" in message:
            return f"{node_prefix}{Fore.GREEN}[artifacts]{Style.RESET_ALL} {message}"

        if agent == "TestDrivenDeveloper" and message.startswith("Implementing against "):
            batch_match = re.match(
                r"Implementing against ([A-Za-z0-9_]+) tests in (\d+) file\(s\) with budget (\d+)\.\.\.",
                message,
            )
            if batch_match:
                return (
                    f"{node_prefix}{Fore.CYAN}[agent]{Style.RESET_ALL} "
                    f"TDD batch · {batch_match.group(1)} · {batch_match.group(2)} file(s) · budget {batch_match.group(3)}"
                )
            return f"{node_prefix}{Fore.CYAN}[agent]{Style.RESET_ALL} TestDrivenDeveloper is implementing against generated tests"
        if agent == "TestDrivenDeveloper" and message.startswith("Modified files after "):
            return f"{node_prefix}{Fore.WHITE}[files]{Style.RESET_ALL} {self._summarize_paths(message, ':')}"
        if agent == "TestDrivenDeveloper" and "ended without explicit IMPLEMENTED" in message:
            return f"{node_prefix}{Fore.YELLOW}[note]{Style.RESET_ALL} TDD session ended without an explicit completion marker; verifying through tests"

        if status == "error":
            return f"{node_prefix}{Fore.RED}[fail]{Style.RESET_ALL} {message}"
        if status == "warning":
            return f"{node_prefix}{Fore.YELLOW}[warn]{Style.RESET_ALL} {message}"
        if status == "info":
            return f"{node_prefix}{Fore.CYAN}[info]{Style.RESET_ALL} {message}"
        if "skipping Test/TDD" in message:
            return f"{node_prefix}{Fore.YELLOW}[skip]{Style.RESET_ALL} {message}"

        if agent in {"Compiler", "System", "RequirementLoader", "InterfaceDesigner", "TestGenerator", "TestDrivenDeveloper"}:
            return f"{node_prefix}{_AGENT_COLORS.get(agent, Fore.WHITE)}[{agent}]{Style.RESET_ALL} {message}"
        return f"{node_prefix}{message}"


_progress_view = CliProgressView()


def init_debug_logger(project_path: str, reset_existing: bool = True) -> str | None:
    global debug_logger, prompt_dump_logger
    arc_dir = os.path.join(project_path, ".arc")
    os.makedirs(arc_dir, exist_ok=True)
    log_path = os.path.join(arc_dir, "debug.log")
    prompt_dump_dir = os.path.join(arc_dir, "prompt_dumps")

    if not ARC_DEBUG_ENABLED:
        debug_logger = None
        prompt_dump_logger = None
        if reset_existing:
            if os.path.isfile(log_path):
                os.remove(log_path)
            if os.path.isdir(prompt_dump_dir):
                shutil.rmtree(prompt_dump_dir, ignore_errors=True)
        return None

    debug_logger = DebugLogger(log_path, reset_existing=reset_existing)
    prompt_dump_logger = PromptDumpLogger(prompt_dump_dir, reset_existing=reset_existing)
    return log_path


def stop_cli_spinner():
    _spinner.stop()


def print_cli_banner():
    banner = "\n".join(
        [
            "",
            f"{Fore.CYAN}+------------------------------------------------------------------+",
            f"|{Style.BRIGHT} ARC Requirement Compiler                                         {Style.RESET_ALL}{Fore.CYAN}|",
            f"|{Fore.WHITE} Compile requirement graphs into interfaces, tests, and code. {Fore.CYAN}|",
            f"|{Fore.WHITE} Multi-agent orchestration · test-driven implementation         {Fore.CYAN}|",
            f"+------------------------------------------------------------------+{Style.RESET_ALL}",
        ]
    )
    print(banner)

def print_cli_startup(
    project_path: str,
    requirement_path: str,
    app_type: str,
    clear_all: bool,
    log_path: str,
    web_port: int | None = None,
    resume_from_queue: bool = False,
):
    from app_types import read_stack_summary

    if log_path:
        print(f"  {Fore.WHITE}Debug Log {Style.RESET_ALL}  {log_path}")
    print(f"\n  {Fore.WHITE}Output    {Style.RESET_ALL}  {project_path}")
    print(f"  {Fore.WHITE}Require   {Style.RESET_ALL}  {requirement_path}")
    print(f"  {Fore.WHITE}App Type  {Style.RESET_ALL}  {Fore.CYAN}{app_type}{Style.RESET_ALL}")
    if app_type == "web" and web_port is not None:
        print(f"  {Fore.WHITE}Web Port  {Style.RESET_ALL}  {web_port}")
    print(f"  {Fore.WHITE}Stack     {Style.RESET_ALL}  {read_stack_summary(project_path, app_type)}")
    mode_label = "resume-compilation" if resume_from_queue else ("clear-and-recompile" if clear_all else "start-compilation")
    print(
        f"  {Fore.WHITE}Mode      {Style.RESET_ALL}  "
        f"{Fore.YELLOW}{mode_label}{Style.RESET_ALL}"
    )
    print(
        f"  {Fore.WHITE}View      {Style.RESET_ALL}  "
        f"{Fore.GREEN if not ARC_DEBUG_ENABLED else Fore.MAGENTA}"
        f"{'concise progress' if not ARC_DEBUG_ENABLED else 'debug'}{Style.RESET_ALL}"
    )
    print(f"  {Fore.WHITE}{'-' * 45}{Style.RESET_ALL}\n")


def print_cli_banner():
    banner = "\n".join(
        [
            "",
            f"{Fore.BLUE}{'=' * 78}{Style.RESET_ALL}",
            f"{Fore.CYAN}{Style.BRIGHT} ARC {Style.RESET_ALL}{Fore.WHITE}Requirement Compiler{Style.RESET_ALL}",
            f"{Fore.WHITE} Compile requirement graphs into interfaces, tests, and runnable code{Style.RESET_ALL}",
            f"{Fore.BLUE}{'=' * 78}{Style.RESET_ALL}",
        ]
    )
    print(banner)


def print_cli_startup(
    project_path: str,
    requirement_path: str,
    app_type: str,
    clear_all: bool,
    log_path: str,
    web_port: int | None = None,
    resume_from_queue: bool = False,
):
    from app_types import read_stack_summary

    mode_label = "resume-compilation" if resume_from_queue else ("clear-and-recompile" if clear_all else "start-compilation")
    stack_summary = read_stack_summary(project_path, app_type)
    requirement_name = os.path.basename(os.path.dirname(requirement_path)) or "requirements"
    view_label = "debug" if ARC_DEBUG_ENABLED else "progress"

    print("")
    print(f"{Fore.WHITE} Session{Style.RESET_ALL}")
    print(f"   {Fore.CYAN}mode      {Style.RESET_ALL}{mode_label}")
    print(f"   {Fore.CYAN}input     {Style.RESET_ALL}{requirement_name}")
    print(f"   {Fore.CYAN}target    {Style.RESET_ALL}{app_type}")
    if app_type == "web" and web_port is not None:
        print(f"   {Fore.CYAN}port      {Style.RESET_ALL}{web_port}")
    print(f"   {Fore.CYAN}stack     {Style.RESET_ALL}{stack_summary}")
    print(f"   {Fore.CYAN}view      {Style.RESET_ALL}{view_label}")
    if ARC_DEBUG_ENABLED and log_path:
        print(f"   {Fore.CYAN}debug log {Style.RESET_ALL}{log_path}")
    print(f"{Fore.BLUE}{'-' * 78}{Style.RESET_ALL}\n")


async def cli_log(agent: str, message: str, status: str = None, node_id: str = None):
    if debug_logger:
        prefix = agent
        if node_id:
            prefix = f"{prefix}:{node_id}"
        if status:
            prefix = f"{prefix}:{status}"
        debug_logger.log(prefix, message)

    if not ARC_DEBUG_ENABLED:
        rendered = _progress_view.render(agent, message, status=status, node_id=node_id)
        if rendered:
            _progress_view.emit_rendered(rendered)
            _progress_view.resume_pending_spinner()
        return

    is_thinking = message.startswith("Thinking...")
    is_tool_call = message.startswith("Calling tool:")
    is_task_done = message == "Task completed."

    if is_thinking:
        step_info = message.replace("Thinking...", "").strip()
        label = f"Thinking {step_info}"
        if node_id:
            label = f"[{node_id}] {label}"
        _spinner.start(label)
        return

    if is_tool_call or is_task_done:
        _spinner.stop()

    print(format_cli_log(agent, message, status=status, node_id=node_id))


def format_cli_log(agent: str, message: str, status: str = None, node_id: str = None) -> str:
    node_prefix = f"{Fore.BLUE}[{node_id}]{Style.RESET_ALL} " if node_id else ""

    if status in _STATUS_COLORS and message == "":
        return f"{node_prefix}{_STATUS_COLORS[status]}[{status}]{Style.RESET_ALL}"

    if status == "error":
        return f"{node_prefix}{Fore.RED}[FAIL] [{agent}] {message}{Style.RESET_ALL}"

    if message.startswith("Calling tool:"):
        tool_match = re.search(r"`(\w+)`", message)
        tool_name = tool_match.group(1) if tool_match else "tool"
        args_match = re.search(r"with args: (.*)", message, re.DOTALL)
        args_summary = ""
        if args_match:
            args_text = args_match.group(1).strip()
            args_summary = f" {args_text[:200]}{'...' if len(args_text) > 200 else ''}"
        return f"{node_prefix}{Fore.YELLOW}> {tool_name}{Style.RESET_ALL}{args_summary}"

    if message.startswith("Thinking..."):
        step_info = message.replace("Thinking...", "").strip()
        return f"{node_prefix}{Fore.CYAN}>> Thinking {step_info}{Style.RESET_ALL}"

    if message == "Task completed.":
        return f"{node_prefix}{Fore.GREEN}[OK] Task completed{Style.RESET_ALL}"

    if message.startswith("Tool `") and ("result:" in message or "result length:" in message):
        if "content not shown" in message:
            return f"{node_prefix}{Fore.WHITE}  {message}{Style.RESET_ALL}"
        if len(message) > 300:
            return f"{node_prefix}{Fore.WHITE}  {message[:300]}...{Style.RESET_ALL}"
        return f"{node_prefix}{Fore.WHITE}  {message}{Style.RESET_ALL}"

    if "Prerequisite check passed" in message:
        return f"{node_prefix}{Fore.GREEN}[OK] {message}{Style.RESET_ALL}"
    if "Prerequisite check FAILED" in message:
        return f"{node_prefix}{Fore.RED}[FAIL] {message}{Style.RESET_ALL}"
    if "skipping Test/TDD" in message:
        return f"{node_prefix}{Fore.YELLOW}[SKIP] {message}{Style.RESET_ALL}"

    if status in _STATUS_COLORS:
        return f"{node_prefix}{_STATUS_COLORS[status]}[{status}]{Style.RESET_ALL} {_AGENT_COLORS.get(agent, Fore.WHITE)}[{agent}]{Style.RESET_ALL} {message}"

    return f"{node_prefix}{_AGENT_COLORS.get(agent, Fore.WHITE)}[{agent}]{Style.RESET_ALL} {message}"


def build_commit_message(node_id: str, phase: str, requirement_data: dict) -> str:
    name = (requirement_data or {}).get("name", "") or ""
    name = name.strip()
    lower_phase = phase.lower()
    if name:
        return f"{node_id} ({lower_phase}): {name}"
    return f"{node_id} ({lower_phase})"


# ======================================================================================
#                        Requirement Loader Utilities
# ======================================================================================

def load_requirements(file_path: str):
    if not os.path.exists(file_path):
        print(f"Requirements file not found: {file_path}")
        return None
    with open(file_path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def dfs_preorder(node: dict) -> List[str]:
    result = []
    node_id = node.get("id")
    if node_id:
        result.append(node_id)
    for child in node.get("children", []):
        result.extend(dfs_preorder(child))
    return result


def is_leaf_node(node: dict) -> bool:
    return not node.get("children", [])


def get_all_leaves(node: dict) -> Dict[str, dict]:
    leaves: Dict[str, dict] = {}
    children = node.get("children", [])
    if not children:
        if node.get("id") != "ROOT":
            leaves[node.get("id")] = node
        return leaves

    for child in children:
        leaves.update(get_all_leaves(child))
    return leaves


def build_dependency_graph(leaves: Dict[str, dict]):
    adjacency = {node_id: [] for node_id in leaves}
    in_degree = {node_id: 0 for node_id in leaves}

    for node_id, node in leaves.items():
        for dependency_id in node.get("dependencies", []):
            if dependency_id in leaves:
                adjacency[dependency_id].append(node_id)
                in_degree[node_id] += 1
            else:
                print(f"Warning: Dependency {dependency_id} for {node_id} not found in leaves.")

    return adjacency, in_degree


def topological_sort(leaves: Dict[str, dict]) -> List[str]:
    adjacency, in_degree = build_dependency_graph(leaves)
    queue = [node_id for node_id in leaves if in_degree[node_id] == 0]
    ordered = []

    while queue:
        current = queue.pop(0)
        ordered.append(current)
        for dependent in adjacency[current]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if len(ordered) != len(leaves):
        print("Error: Cycle detected or disconnected graph issues.")
        remaining = set(leaves.keys()) - set(ordered)
        return ordered + list(remaining)
    return ordered


def detect_requirement_path(project_path: str, requirement_path: Optional[str]) -> str:
    if requirement_path:
        if os.path.isabs(requirement_path):
            return requirement_path
        return os.path.abspath(os.path.join(project_path, requirement_path))

    candidates = [
        os.path.join(project_path, "requirements", "requirements.yaml"),
        os.path.join(project_path, "requirements", "requirents.yaml"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "Could not find requirements file. Tried: requirements/requirements.yaml, requirements/requirents.yaml"
    )
