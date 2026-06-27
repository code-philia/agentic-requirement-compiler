import os
import re
import sys
import time
import yaml
import asyncio
import threading

from typing import Awaitable, Callable, Optional, Dict, List
from colorama import Fore, Style, init as colorama_init

from app_context import (
    get_abs_path,
    get_android_package,
    get_app_type,
    set_android_package,
    set_app_type,
    set_workspace_root,
)
from prompts.stack import read_stack_summary

colorama_init()

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
    def __init__(self, log_path: str):
        self._path = log_path
        self._lock = threading.Lock()
        with open(self._path, "w", encoding="utf-8") as file:
            file.write(f"=== ARC Debug Log | {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n\n")

    def log(self, tag: str, content: str):
        timestamp = time.strftime("%H:%M:%S")
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as file:
                file.write(f"[{timestamp}] [{tag}] {content}\n")


_spinner = Spinner()
debug_logger: Optional[DebugLogger] = None

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


def init_debug_logger(project_path: str) -> str:
    global debug_logger
    arc_dir = os.path.join(project_path, ".arc")
    os.makedirs(arc_dir, exist_ok=True)
    log_path = os.path.join(arc_dir, "debug.log")
    debug_logger = DebugLogger(log_path)
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


def print_cli_startup(project_path: str, requirement_path: str, app_type: str, clear_all: bool, log_path: str):
    print(f"  {Fore.WHITE}Debug Log {Style.RESET_ALL}  {log_path}")
    print(f"\n  {Fore.WHITE}Project   {Style.RESET_ALL}  {project_path}")
    print(f"  {Fore.WHITE}Require   {Style.RESET_ALL}  {requirement_path}")
    print(f"  {Fore.WHITE}App Type  {Style.RESET_ALL}  {Fore.CYAN}{app_type}{Style.RESET_ALL}")
    print(f"  {Fore.WHITE}Stack     {Style.RESET_ALL}  {read_stack_summary(project_path)}")
    print(
        f"  {Fore.WHITE}Mode      {Style.RESET_ALL}  "
        f"{Fore.YELLOW}{'clear-and-recompile' if clear_all else 'start-compilation'}{Style.RESET_ALL}"
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
#                                   Commands
# ======================================================================================

async def run_npm_install(target_dir: str, log_cb: Callable[..., Awaitable[None]]):
    try:
        process = await asyncio.create_subprocess_shell(
            "npm install",
            cwd=target_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode == 0:
            await log_cb("System", f"NPM install success in {target_dir}")
        else:
            await log_cb("System", f"NPM install failed in {target_dir}: {stderr.decode()}")
    except Exception as exc:
        await log_cb("System", f"NPM install error: {str(exc)}")


async def run_git_init(target_dir: str, log_cb: Callable[..., Awaitable[None]]):
    try:
        process = await asyncio.create_subprocess_shell(
            "git init",
            cwd=target_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()

        process = await asyncio.create_subprocess_shell(
            "git add .",
            cwd=target_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()

        process = await asyncio.create_subprocess_shell(
            'git commit -m "init"',
            cwd=target_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode == 0:
            await log_cb("System", f"Git initialized and committed 'init' in {target_dir}")
        else:
            await log_cb("System", f"Git init/commit failed: {stderr.decode()}")
    except Exception as exc:
        await log_cb("System", f"Git init error: {str(exc)}")


async def run_git_commit(target_dir: str, message: str, log_cb: Callable[..., Awaitable[None]]):
    try:
        process = await asyncio.create_subprocess_shell(
            "git add .",
            cwd=target_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()

        safe_message = message.replace('"', '\\"')
        process = await asyncio.create_subprocess_shell(
            f'git commit -m "{safe_message}"',
            cwd=target_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            await log_cb("System", f"Git commit success: '{message}'")
            return

        stderr_text = stderr.decode()
        stdout_text = stdout.decode()
        if "nothing to commit" in stdout_text or "nothing to commit" in stderr_text:
            await log_cb("System", "Git commit skipped (nothing to commit).")
        else:
            await log_cb("System", f"Git commit failed: {stderr_text}")
    except Exception as exc:
        await log_cb("System", f"Git commit error: {str(exc)}")


def build_commit_message(node_id: str, phase: str, requirement_data: dict) -> str:
    name = (requirement_data or {}).get("name", "") or ""
    name = name.strip()
    if name:
        return f"{node_id} {name} ({phase})"
    return f"{node_id} ({phase})"


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
