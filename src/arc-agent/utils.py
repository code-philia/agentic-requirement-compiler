import os
import re
import sys
import time
import yaml
import json
import asyncio
import threading
import copy

from typing import Awaitable, Callable, Optional, Dict, List, Any
from colorama import Fore, Style, init as colorama_init
from traceability.database import get_interfaces_by_req_id, get_requirement_by_id

colorama_init()

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
    req = get_requirement_by_id(node_id)
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
        dep_req = get_requirement_by_id(dep_id)
        if not dep_req:
            continue

        lines.append(f"#### Dependency Requirement Node: [{dep_id}]")
        lines.append(f"Description: {dep_req.get('description', 'N/A')}")

        dep_ifaces = get_interfaces_by_req_id(dep_id)
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
                if func.get("name") == "write_file" or (
                    hasattr(func, "name") and func.name == "write_file"
                ):
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

    def __init__(self):
        self._active = False
        self._thread = None
        self._text = ""

    def start(self, text: str = "Thinking"):
        if self._active:
            self._text = text
            return
        self._active = True
        self._text = text
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self):
        self._active = False
        if self._thread:
            self._thread.join(timeout=0.5)
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()

    def _spin(self):
        idx = 0
        while self._active:
            frame = self.FRAMES[idx % len(self.FRAMES)]
            sys.stdout.write(f"\r  {Fore.CYAN}{frame}{Style.RESET_ALL} {self._text}...")
            sys.stdout.flush()
            idx += 1
            threading.Event().wait(0.08)


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

ARC_GITIGNORE_START = "# ARC_MANAGED_START"
ARC_GITIGNORE_END = "# ARC_MANAGED_END"
DEFAULT_GIT_USER_NAME = "ARC Agent"
DEFAULT_GIT_USER_EMAIL = "arc-agent@local.invalid"

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
}


def _get_git_identity() -> tuple[str, str]:
    user_name = (
        os.environ.get("ARC_GIT_USER_NAME")
        or os.environ.get("GIT_AUTHOR_NAME")
        or os.environ.get("GIT_COMMITTER_NAME")
        or DEFAULT_GIT_USER_NAME
    ).strip()
    user_email = (
        os.environ.get("ARC_GIT_USER_EMAIL")
        or os.environ.get("GIT_AUTHOR_EMAIL")
        or os.environ.get("GIT_COMMITTER_EMAIL")
        or DEFAULT_GIT_USER_EMAIL
    ).strip()
    return user_name, user_email


def _build_git_env() -> dict[str, str]:
    env = os.environ.copy()
    user_name, user_email = _get_git_identity()
    env["GIT_AUTHOR_NAME"] = user_name
    env["GIT_AUTHOR_EMAIL"] = user_email
    env["GIT_COMMITTER_NAME"] = user_name
    env["GIT_COMMITTER_EMAIL"] = user_email
    return env


async def _run_git_command(
    args: list[str],
    cwd: str,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode(), stderr.decode()


async def _configure_git_identity(target_dir: str) -> tuple[bool, str]:
    user_name, user_email = _get_git_identity()
    code, _, stderr = await _run_git_command(
        ["git", "config", "user.name", user_name],
        cwd=target_dir,
    )
    if code != 0:
        return False, stderr

    code, _, stderr = await _run_git_command(
        ["git", "config", "user.email", user_email],
        cwd=target_dir,
    )
    if code != 0:
        return False, stderr

    return True, f"{user_name} <{user_email}>"


def init_debug_logger(project_path: str, reset_existing: bool = True) -> str:
    global debug_logger, prompt_dump_logger
    arc_dir = os.path.join(project_path, ".arc")
    os.makedirs(arc_dir, exist_ok=True)
    log_path = os.path.join(arc_dir, "debug.log")
    debug_logger = DebugLogger(log_path, reset_existing=reset_existing)
    prompt_dump_logger = PromptDumpLogger(
        os.path.join(arc_dir, "prompt_dumps"),
        reset_existing=reset_existing,
    )
    return log_path


def stop_cli_spinner():
    _spinner.stop()


def print_cli_banner():
    banner = "\n".join(
        [
            f"{Fore.CYAN}============================================",
            "        ARC Requirement Compiler",
            "      Agentic | Multi-Agent | TDD",
            f"============================================{Style.RESET_ALL}",
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
    print(f"  {Fore.WHITE}{'-' * 45}{Style.RESET_ALL}\n")


async def cli_log(agent: str, message: str, status: str = None, node_id: str = None):
    if debug_logger:
        prefix = agent
        if node_id:
            prefix = f"{prefix}:{node_id}"
        if status:
            prefix = f"{prefix}:{status}"
        debug_logger.log(prefix, message)

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


# ======================================================================================
#                                   Git commands
# ======================================================================================

async def run_git_init(target_dir: str, log_cb: Callable[..., Awaitable[None]]):
    try:
        git_env = _build_git_env()

        code, _, stderr = await _run_git_command(["git", "init"], cwd=target_dir, env=git_env)
        if code != 0:
            await log_cb("System", f"Git init failed: {stderr}")
            return

        configured, identity_message = await _configure_git_identity(target_dir)
        if not configured:
            await log_cb("System", f"Git identity configuration failed: {identity_message}")
            return

        await log_cb("System", f"Using git identity: {identity_message}")

        code, _, stderr = await _run_git_command(["git", "add", "."], cwd=target_dir, env=git_env)
        if code != 0:
            await log_cb("System", f"Git add failed during init: {stderr}")
            return

        code, _, stderr = await _run_git_command(
            ["git", "commit", "-m", "init"],
            cwd=target_dir,
            env=git_env,
        )

        if code == 0:
            await log_cb("System", f"Git initialized and committed 'init' in {target_dir}")
        else:
            await log_cb("System", f"Git init/commit failed: {stderr}")
    except Exception as exc:
        await log_cb("System", f"Git init error: {str(exc)}")

def ensure_arc_gitignore(project_path: str) -> str:
    gitignore_path = os.path.join(project_path, ".gitignore")
    managed_block = "\n".join(
        [
            ARC_GITIGNORE_START,
            "backend/node_modules/",
            "frontend/node_modules/",
            "backend/coverage/",
            "frontend/dist/",
            "frontend/dist-ssr/",
            "*.db",
            ".env",
            "!.arc/",
            "!.arc/**",
            ".arc/debug.log",
            ARC_GITIGNORE_END,
        ]
    )

    old_content = ""
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8") as file:
            old_content = file.read()

    start = old_content.find(ARC_GITIGNORE_START)
    end = old_content.find(ARC_GITIGNORE_END)
    if start != -1 and end != -1 and end > start:
        before = old_content[:start].rstrip()
        after = old_content[end + len(ARC_GITIGNORE_END):].lstrip()
        merged = ""
        if before:
            merged += before + "\n\n"
        merged += managed_block
        if after:
            merged += "\n\n" + after
        content = merged.strip() + "\n"
    elif old_content.strip():
        content = old_content.rstrip() + "\n\n" + managed_block + "\n"
    else:
        content = managed_block + "\n"

    with open(gitignore_path, "w", encoding="utf-8") as file:
        file.write(content)
    return gitignore_path


async def run_git_commit(target_dir: str, message: str, log_cb: Callable[..., Awaitable[None]]):
    try:
        git_env = _build_git_env()

        code, _, stderr = await _run_git_command(["git", "add", "."], cwd=target_dir, env=git_env)
        if code != 0:
            await log_cb("System", f"Git add failed: {stderr}")
            return

        code, stdout_text, stderr_text = await _run_git_command(
            ["git", "commit", "-m", message],
            cwd=target_dir,
            env=git_env,
        )

        if code == 0:
            await log_cb("System", f"Git commit success: '{message}'")
            return

        if "nothing to commit" in stdout_text or "nothing to commit" in stderr_text:
            await log_cb("System", "Git commit skipped (nothing to commit).")
        else:
            await log_cb("System", f"Git commit failed: {stderr_text}")
    except Exception as exc:
        await log_cb("System", f"Git commit error: {str(exc)}")


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
