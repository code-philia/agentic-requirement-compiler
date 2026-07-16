from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def format_json_for_log(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        return repr(value)


def log_to_logger(logger: Any | None, event: str, *, label: str, thread_id: str, body: str = "") -> None:
    if logger is None:
        return
    message = f"{event} label={label or '-'} thread_id={thread_id}"
    if body:
        message = f"{message}\n{body}"
    if hasattr(logger, "info"):
        logger.info("%s", message)
    elif callable(logger):
        logger(message)


def append_debug_log(
    agent_name: str,
    message: str,
    *,
    status: str | None = None,
    node_id: str | None = None,
    workspace_root: str | None = None,
) -> None:
    log_path = _resolve_debug_log_path(workspace_root)
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = local_timestamp()
    prefix = f"[{timestamp}] [{agent_name}]"
    if node_id:
        prefix += f"[{node_id}]"
    if status:
        prefix += f"[{status}]"
    lines = str(message).splitlines() or [""]
    with log_path.open("a", encoding="utf-8") as file:
        for line in lines:
            file.write(f"{prefix} {line}\n")


def format_terminal_log(
    agent_name: str,
    message: str,
    *,
    status: str | None = None,
    node_id: str | None = None,
) -> str:
    timestamp = local_timestamp()
    prefix = f"[{timestamp}] [{agent_name}]"
    if node_id:
        prefix += f"[{node_id}]"
    if status:
        prefix += f"[{status}]"
    if not _should_color():
        return "\n".join(f"{prefix} {line}" for line in (str(message).splitlines() or [""]))

    prefix_color = _agent_color(agent_name, message, status)
    colored_prefix = f"{prefix_color}{prefix}{ANSI_RESET}"
    return "\n".join(
        f"{colored_prefix} {_color_message(line, status)}"
        for line in (str(message).splitlines() or [""])
    )


def write_terminal_log(
    agent_name: str,
    message: str,
    *,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    print(format_terminal_log(agent_name, message, status=status, node_id=node_id), flush=True)


ANSI_RESET = "\033[0m"
ANSI_DIM = "\033[2m"
ANSI_BOLD = "\033[1m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"
ANSI_GRAY = "\033[90m"


def local_timestamp() -> str:
    return datetime.now(_log_timezone()).isoformat(timespec="milliseconds")


def _log_timezone() -> tzinfo:
    configured = os.environ.get("ARC_TIMEZONE", "").strip() or os.environ.get("TZ", "").strip()
    if configured:
        try:
            return ZoneInfo(configured)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _resolve_debug_log_path(workspace_root: str | None) -> Path | None:
    explicit = os.environ.get("ARC_DEBUG_LOG_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    root = workspace_root or os.environ.get("ARC_WORKSPACE_ROOT", "").strip()
    if not root:
        return None
    return Path(root).expanduser().resolve() / ".arc" / "debug.log"


def _should_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    forced = os.environ.get("ARC_LOG_COLOR", "").strip().lower()
    if forced in {"1", "true", "yes", "on"}:
        return True
    if forced in {"0", "false", "no", "off"}:
        return False
    return sys.stdout.isatty()


def _agent_color(agent_name: str, message: str, status: str | None) -> str:
    normalized = f"{agent_name} {message} {status or ''}".lower()
    if status == "error" or "failed" in normalized:
        return ANSI_RED + ANSI_BOLD
    if status == "warning":
        return ANSI_YELLOW + ANSI_BOLD
    if "tool-call>" in normalized:
        return ANSI_CYAN + ANSI_BOLD
    if "tool-result>" in normalized:
        return ANSI_GREEN + ANSI_BOLD
    if "model>" in normalized or "model-final>" in normalized:
        return ANSI_MAGENTA + ANSI_BOLD
    if "deep-agent" in normalized or "agent trace" in normalized:
        return ANSI_BLUE + ANSI_BOLD
    if agent_name in {"System", "Compiler", "RequirementLoader"}:
        return ANSI_GRAY + ANSI_BOLD
    return ANSI_BOLD


def _color_message(message: str, status: str | None) -> str:
    if status == "error":
        return f"{ANSI_RED}{message}{ANSI_RESET}"
    if status == "warning":
        return f"{ANSI_YELLOW}{message}{ANSI_RESET}"
    if message.startswith("tool-call>"):
        return f"{ANSI_CYAN}{message}{ANSI_RESET}"
    if message.startswith("tool-result>"):
        return f"{ANSI_GREEN}{message}{ANSI_RESET}"
    if message.startswith("model>") or message.startswith("model-final>"):
        return f"{ANSI_MAGENTA}{message}{ANSI_RESET}"
    if message.startswith("agent trace:"):
        return f"{ANSI_DIM}{message}{ANSI_RESET}"
    return message
