import argparse
import asyncio
import os
import re
import sys
import threading
import time
from typing import Optional, Dict, Any

try:
    from colorama import init, Fore, Style
    init()
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False
    # Fallback: no color
    class _Fore:
        RED = CYAN = GREEN = YELLOW = MAGENTA = BLUE = WHITE = RESET = ""
    class _Style:
        BRIGHT = RESET_ALL = ""
    Fore = _Fore()
    Style = _Style()

ARC_STACK_START = "<!-- ARC_TECH_STACK_START -->"
ARC_STACK_END = "<!-- ARC_TECH_STACK_END -->"


# ============================================================
# Spinner animation for LLM waiting
# ============================================================

class Spinner:
    """Lightweight spinner shown while waiting for LLM responses."""

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

    def update(self, text: str):
        self._text = text

    def stop(self):
        self._active = False
        if self._thread:
            self._thread.join(timeout=0.5)
        # Clear the spinner line
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    def _spin(self):
        idx = 0
        while self._active:
            frame = self.FRAMES[idx % len(self.FRAMES)]
            sys.stdout.write(f"\r  {Fore.CYAN}{frame}{Style.RESET_ALL} {self._text}...")
            sys.stdout.flush()
            idx += 1
            threading.Event().wait(0.08)


_spinner = Spinner()


# ============================================================
# Debug logger — appends all output to a .log file
# ============================================================

class DebugLogger:
    """Thread-safe file logger for debug mode. Appends every message to a .log file."""

    def __init__(self, log_path: str):
        self._path = log_path
        self._lock = threading.Lock()
        with open(self._path, "w", encoding="utf-8") as f:
            f.write(f"=== ARC Debug Log — {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n\n")

    def log(self, tag: str, content: str):
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [{tag}] {content}\n")

    def log_raw(self, content: str):
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(content + "\n")

    def log_broadcast(self, message: Dict[str, Any]):
        """Log a broadcast message (the full dict)."""
        msg_type = message.get("type", "log")
        agent = message.get("agent", "")
        node_id = message.get("nodeId", "")
        msg_text = message.get("message", "")
        status = message.get("status", "")

        prefix = f"[{msg_type}]"
        if agent:
            prefix += f" [{agent}]"
        if node_id:
            prefix += f" [{node_id}]"
        if status:
            prefix += f" [{status}]"

        self.log(prefix, msg_text)


# Global debug logger instance (None until initialized)
debug_logger: Optional[DebugLogger] = None


# ============================================================
# Colored log formatting
# ============================================================

_AGENT_COLORS = {
    "System":          Fore.WHITE,
    "RequirementLoader": Fore.YELLOW,
    "DependencyManager": Fore.YELLOW,
    "InterfaceDesigner": Fore.MAGENTA,
    "TestGenerator":    Fore.GREEN,
    "TestDrivenDeveloper": Fore.CYAN,
}

_STATUS_COLORS = {
    "analyzing": Fore.YELLOW,
    "designed":  Fore.MAGENTA,
    "completed": Fore.GREEN,
    "error":     Fore.RED,
}


def _format_log(message: Dict[str, Any]) -> str:
    msg_type = message.get("type", "log")
    agent = message.get("agent", "System")
    node_id = message.get("nodeId", "")
    status = message.get("status", "")
    msg_text = message.get("message", "")

    if msg_type == "node_update":
        status_color = _STATUS_COLORS.get(status, Fore.WHITE)
        node_prefix = f"{Fore.BLUE}[{node_id}]{Style.RESET_ALL} " if node_id else ""
        return f"{node_prefix}{status_color}[{status}]{Style.RESET_ALL}"

    if msg_type == "error-event":
        node_prefix = f"{Fore.BLUE}[{node_id}]{Style.RESET_ALL} " if node_id else ""
        return f"{node_prefix}{Fore.RED}[FAIL] [{agent}] {msg_text}{Style.RESET_ALL}"

    if msg_type == "db_update":
        data = message.get("data", {})
        return f"{Fore.BLUE}[DB]{Style.RESET_ALL} [{agent}] table={data.get('table', '?')} items={data.get('items', '?')}"

    if msg_type == "clear-logs":
        return f"\n{Fore.WHITE}{'─' * 50}{Style.RESET_ALL}"

    # Regular log
    agent_color = _AGENT_COLORS.get(agent, Fore.WHITE)
    node_prefix = f"{Fore.BLUE}[{node_id}]{Style.RESET_ALL} " if node_id else ""

    # Detect special messages for spinner control
    is_thinking = msg_text.startswith("Thinking...")
    is_tool_call = msg_text.startswith("Calling tool:")
    is_task_done = msg_text == "Task completed."

    if is_thinking:
        step_info = msg_text.replace("Thinking...", "").strip()
        return f"{node_prefix}{Fore.CYAN}>> Thinking {step_info}{Style.RESET_ALL}"

    if is_tool_call:
        # Extract tool name
        tool_match = re.search(r'`(\w+)`', msg_text)
        tool_name = tool_match.group(1) if tool_match else "tool"
        return f"{node_prefix}{Fore.YELLOW}> {tool_name}{Style.RESET_ALL}"

    if is_task_done:
        return f"{node_prefix}{Fore.GREEN}[OK] Task completed{Style.RESET_ALL}"

    # Prerequisite check messages
    if "Prerequisite check passed" in msg_text:
        return f"{node_prefix}{Fore.GREEN}[OK] {msg_text}{Style.RESET_ALL}"
    if "Prerequisite check FAILED" in msg_text:
        return f"{node_prefix}{Fore.RED}[FAIL] {msg_text}{Style.RESET_ALL}"

    # Non-leaf skip message
    if "skipping Test/TDD" in msg_text:
        return f"{node_prefix}{Fore.YELLOW}[SKIP] {msg_text}{Style.RESET_ALL}"

    # Default
    return f"{node_prefix}{agent_color}[{agent}]{Style.RESET_ALL} {msg_text}"


# ============================================================
# Broadcast callback with spinner integration
# ============================================================

async def _console_broadcast(message: Dict[str, Any]):
    msg_type = message.get("type", "log")
    msg_text = message.get("message", "")

    # Always log to debug file (full content, no truncation)
    if debug_logger:
        debug_logger.log_broadcast(message)

    # Spinner control: start on "Thinking...", stop on tool call or task done
    is_thinking = msg_text.startswith("Thinking...")
    is_tool_call = msg_text.startswith("Calling tool:")
    is_task_done = msg_text == "Task completed."

    if is_thinking:
        step_info = msg_text.replace("Thinking...", "").strip()
        node_id = message.get("nodeId", "")
        label = f"Thinking {step_info}"
        if node_id:
            label = f"[{node_id}] {label}"
        _spinner.start(label)
        return  # Don't print — spinner handles the display

    if is_tool_call or is_task_done:
        _spinner.stop()

    # Print the formatted line
    print(_format_log(message))


# ============================================================
# Requirement path detection
# ============================================================

def _detect_requirement_path(project_path: str, requirement_path: Optional[str]) -> str:
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


def _read_stack_summary(project_path: str) -> str:
    metadata_path = os.path.join(project_path, ".arc", "metadata.md")
    if not os.path.exists(metadata_path):
        return "No .arc/metadata.md found. Stack defaults will be inferred by templates/tools."

    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            content = f.read()
        backend = re.search(r"-\s*backend:\s*(.+)", content, re.IGNORECASE)
        frontend = re.search(r"-\s*frontend:\s*(.+)", content, re.IGNORECASE)
        database = re.search(r"-\s*database:\s*(.+)", content, re.IGNORECASE)
        platform = re.search(r"\*\*\s*Platform\s*\*\*\s*:\s*(.+)", content, re.IGNORECASE)
        if platform:
            return f"platform={platform.group(1).strip()}"
        return (
            f"backend={backend.group(1).strip() if backend else 'N/A'}, "
            f"frontend={frontend.group(1).strip() if frontend else 'N/A'}, "
            f"database={database.group(1).strip() if database else 'N/A'}"
        )
    except Exception as e:
        return f"Failed to parse metadata.md: {str(e)}"


# ============================================================
# Stack metadata
# ============================================================

def _build_stack_block(app_type: str) -> str:
    if app_type == "android":
        return "\n".join(
            [
                ARC_STACK_START,
                "* **Platform** : Android Native App (Single-module `app` template)",
                "* **Build System** : Gradle Wrapper + Android Gradle Plugin `8.1.4`",
                "* **Language** : Java 8 (`sourceCompatibility` / `targetCompatibility` = 1.8)",
                "* **UI Stack** : XML Layout + AndroidX AppCompat + Material Components + ConstraintLayout",
                "* **SDK Target** : `compileSdk 34` / `minSdk 31` / `targetSdk 34`",
                "* **Runtime Entry** : `MainActivity` + `AndroidManifest.xml`",
                "* **Database** : Room 2.4.3 (runtime + annotation processor)",
                "* **Testing (Unit)** : JUnit5 + Robolectric (`app/src/test/`)",
                "* **Testing (Integration)** : Robolectric + MockWebServer + Room in-memory DB (`app/src/test/`)",
                "* **Testing (E2E)** : Robolectric + MockWebServer (`app/src/test/`)",
                ARC_STACK_END,
            ]
        )

    # default: web
    return "\n".join(
        [
            ARC_STACK_START,
            "### Main Stack",
            "- backend: nodejs",
            "- frontend: react",
            "- database: sqlite",
            "",
            "### Frontend",
            "* **Framework**: React 18+ (Vite)",
            "* **Language**: JavaScript (ES6+)",
            "* **Styling**: Tailwind CSS v4",
            "* **HTTP**: Axios (Must use Interceptors for global error handling)",
            "* **Testing**: None in frontend directory. (Verified via E2E in backend).",
            "",
            "### Backend",
            "* **Runtime**: Node.js (LTS)",
            "* **Framework**: Express.js",
            "* **Database**: SQLite3 (`sqlite3` driver, file-based)",
            "* **Testing**:",
            "  * Vitest: Used for Unit and Integration testing.",
            "  * Supertest: Used with Vitest for API route testing.",
            "  * Playwright: Used for End-to-End (E2E) testing, located in `backend/test-e2e`.",
            ARC_STACK_END,
        ]
    )


def _upsert_metadata(project_path: str, app_type: str) -> str:
    arc_dir = os.path.join(project_path, ".arc")
    os.makedirs(arc_dir, exist_ok=True)
    metadata_path = os.path.join(arc_dir, "metadata.md")
    new_block = _build_stack_block(app_type)

    old = ""
    if os.path.exists(metadata_path):
        with open(metadata_path, "r", encoding="utf-8") as f:
            old = f.read()

    start = old.find(ARC_STACK_START)
    end = old.find(ARC_STACK_END)
    if start != -1 and end != -1 and end > start:
        before = old[:start].rstrip()
        after = old[end + len(ARC_STACK_END):].lstrip()
        merged = ""
        if before:
            merged += before + "\n\n"
        merged += new_block
        if after:
            merged += "\n\n" + after
        content = merged.strip() + "\n"
    elif old.strip():
        content = old.rstrip() + "\n\n" + new_block + "\n"
    else:
        content = new_block + "\n"

    with open(metadata_path, "w", encoding="utf-8") as f:
        f.write(content)
    return metadata_path


# ============================================================
# Main run logic
# ============================================================

def _print_banner():
    logo = f"""{Fore.CYAN}
    ╔═══════════════════════════════════════╗
    ║          ARC Requirement Compiler      ║
    ║        Agentic · Multi-Agent · TDD     ║
    ╚═══════════════════════════════════════╝{Style.RESET_ALL}"""
    print(logo)


async def _run(project_path: str, requirement_path: Optional[str], clear_all: bool, app_type: str):
    global debug_logger
    import main as arc_main

    project_path = os.path.abspath(project_path)
    if not os.path.isdir(project_path):
        raise FileNotFoundError(f"Project path does not exist: {project_path}")

    req_path = _detect_requirement_path(project_path, requirement_path)
    if not os.path.exists(req_path):
        raise FileNotFoundError(f"Requirement file does not exist: {req_path}")

    metadata_path = _upsert_metadata(project_path, app_type)

    # Initialize debug logger
    arc_dir = os.path.join(project_path, ".arc")
    os.makedirs(arc_dir, exist_ok=True)
    log_path = os.path.join(arc_dir, "debug.log")
    debug_logger = DebugLogger(log_path)
    print(f"  {Fore.WHITE}Debug Log {Style.RESET_ALL}  {log_path}")

    print(f"\n  {Fore.WHITE}Project   {Style.RESET_ALL}  {project_path}")
    print(f"  {Fore.WHITE}Require   {Style.RESET_ALL}  {req_path}")
    print(f"  {Fore.WHITE}App Type  {Style.RESET_ALL}  {Fore.CYAN}{app_type}{Style.RESET_ALL}")
    print(f"  {Fore.WHITE}Stack     {Style.RESET_ALL}  {_read_stack_summary(project_path)}")
    print(f"  {Fore.WHITE}Mode      {Style.RESET_ALL}  {Fore.YELLOW}{'clear-and-recompile' if clear_all else 'start-compilation'}{Style.RESET_ALL}")
    print(f"  {Fore.WHITE}{'─' * 45}{Style.RESET_ALL}\n")

    original_broadcast = arc_main.manager.broadcast
    arc_main.manager.broadcast = _console_broadcast
    try:
        await arc_main.run_compilation(
            project_path=project_path,
            requirement_path=req_path,
            clear_all=clear_all,
            app_type=app_type,
        )
    finally:
        _spinner.stop()
        arc_main.manager.broadcast = original_broadcast


# ============================================================
# CLI entry point
# ============================================================

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run ARC agent workflow directly from terminal (no websocket UI needed)."
    )
    parser.add_argument(
        "project_path",
        nargs="?",
        help="Target project root path (contains requirements/ and .arc/). If omitted, will prompt interactively.",
    )
    parser.add_argument(
        "--requirement-path",
        help="Requirement yaml path. Absolute path, or relative to project path.",
    )
    parser.add_argument(
        "--clear-all",
        action="store_true",
        help="Clear project workspace and recompile (same semantics as 'Clear and Restart Compilation').",
    )
    parser.add_argument(
        "--app-type",
        choices=["web", "android"],
        default="web",
        help="Application type for stack metadata writing (default: web).",
    )
    return parser.parse_args()


def main():
    args = _parse_args()
    project_path = args.project_path
    if not project_path:
        project_path = input("Enter target project path: ").strip()
    if not project_path:
        raise ValueError("Target project path is required.")

    _print_banner()
    asyncio.run(_run(project_path, args.requirement_path, args.clear_all, args.app_type))


if __name__ == "__main__":
    main()