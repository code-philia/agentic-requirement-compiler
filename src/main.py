from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from app_type_handler import list_app_types, normalize_app_type
from core.utils import (
    cli_log,
    init_debug_logger,
    print_cli_banner,
    print_cli_startup,
    print_compilation_summary,
    set_web_port,
    stop_cli_spinner,
)
from core.workflow import ARCWorkflowManager


@dataclass(slots=True)
class CompilationConfig:
    output_dir: str
    requirement_dir: str
    requirement_path: str
    user_requested_clear_all: bool = False
    app_type: str = "web"
    web_port: int = 3301
    resume_from_queue: bool = False
    retry_failed: bool = False
    retry_node_ids: list[str] | None = None
    model_api_mode: str | None = None


def _get_repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _ensure_dotenv_loaded() -> None:
    """Load .env file if present, respecting ARC_ENV_FILE override."""
    from dotenv import load_dotenv

    custom_env = os.environ.get("ARC_ENV_FILE", "").strip()
    if custom_env and os.path.isfile(custom_env):
        load_dotenv(custom_env, override=False)
        return

    repo_root = _get_repo_root()
    default_env = os.path.join(repo_root, ".env")
    if os.path.isfile(default_env):
        load_dotenv(default_env, override=False)


def _locate_requirement_file(input_path: str) -> tuple[str, str, str]:
    """
    Locate requirements.yaml given an input path.
    Returns: (requirement_dir, requirement_path, requirement_name)
    """
    abs_input = os.path.abspath(input_path)

    if os.path.isfile(abs_input):
        if not abs_input.endswith((".yaml", ".yml")):
            raise ValueError(f"Input file must be .yaml or .yml: {abs_input}")
        requirement_dir = os.path.dirname(abs_input)
        requirement_path = abs_input
        requirement_name = os.path.basename(abs_input)
        return requirement_dir, requirement_path, requirement_name

    if os.path.isdir(abs_input):
        # Input directory should directly contain requirements.yaml
        candidates = ["requirements.yaml", "requirements.yml"]
        for candidate in candidates:
            candidate_path = os.path.join(abs_input, candidate)
            if os.path.isfile(candidate_path):
                return abs_input, candidate_path, candidate
        raise FileNotFoundError(f"No requirements.yaml found in {abs_input}")

    raise FileNotFoundError(f"Input path not found: {abs_input}")


# ============================================================
# Subcommand: compile
# ============================================================
def build_compile_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "compile",
        help="Compile requirements into a working application",
        description="Run ARC compilation from requirement tree to interfaces, tests, and implementation.",
    )
    parser.add_argument(
        "requirement_path",
        help="Path to requirements directory or .yaml file",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        required=True,
        help="Output workspace directory",
    )
    parser.add_argument(
        "-t",
        "--type",
        dest="app_type",
        default="web",
        help=f"Application type (choices: {', '.join(list_app_types())})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3301,
        help="Web server port (only for app-type=web, default: 3301)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove existing output directory before compilation",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from saved compilation queue",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry all failed nodes from previous run (requires --resume)",
    )
    parser.add_argument(
        "--retry",
        nargs="+",
        metavar="NODE_ID",
        help="Retry specific node IDs (requires --resume)",
    )
    parser.set_defaults(func=cmd_compile)


async def cmd_compile(args: argparse.Namespace) -> int:
    """Execute compile subcommand."""
    _ensure_dotenv_loaded()
    
    # Validate mutual exclusivity
    if args.clean and args.resume:
        print("Error: --clean and --resume are mutually exclusive")
        return 2
    if (args.retry_failed or args.retry) and not args.resume:
        print("Error: --retry-failed and --retry require --resume")
        return 2
    if args.retry_failed and args.retry:
        print("Error: --retry-failed and --retry are mutually exclusive")
        return 2
    
    # Normalize paths
    requirement_dir, requirement_path, _ = _locate_requirement_file(args.requirement_path)
    output_dir = os.path.abspath(args.output_dir)
    
    # Handle --clean
    if args.clean and os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    
    # Normalize app type
    normalized_app_type = normalize_app_type(args.app_type)
    
    # Set web port
    set_web_port(args.port)
    
    # Model API mode
    model_api_mode = os.environ.get("ARC_OPENAI_API_MODE", "").strip() or None
    
    config = CompilationConfig(
        output_dir=output_dir,
        requirement_dir=requirement_dir,
        requirement_path=requirement_path,
        user_requested_clear_all=args.clean,
        app_type=normalized_app_type,
        web_port=args.port,
        resume_from_queue=args.resume,
        retry_failed=args.retry_failed,
        retry_node_ids=args.retry or None,
        model_api_mode=model_api_mode,
    )
    
    # Print banner and startup info
    print_cli_banner()
    log_path = init_debug_logger(config.output_dir, reset_existing=not config.resume_from_queue)
    print_cli_startup(
        project_path=config.output_dir,
        requirement_path=config.requirement_path,
        app_type=config.app_type,
        clear_all=config.user_requested_clear_all,
        log_path=log_path,
        web_port=config.web_port,
        resume_from_queue=config.resume_from_queue,
        retry_failed=config.retry_failed,
        retry_node_ids=config.retry_node_ids,
        model_api_mode=config.model_api_mode,
    )
    
    # Run compilation
    start_time = time.time()
    try:
        workflow_manager = ARCWorkflowManager(
            workspace_path=config.output_dir,
            requirement_path=config.requirement_path,
            app_type=config.app_type,
            web_port=config.web_port,
            log_cb=cli_log,
        )
        result = await workflow_manager.start_compilation(
            clear_all=False,
            resume_from_queue=config.resume_from_queue,
            retry_failed=config.retry_failed,
            retry_node_ids=config.retry_node_ids,
        )
    finally:
        stop_cli_spinner()
    
    elapsed = time.time() - start_time
    print_compilation_summary(result, config.output_dir, elapsed)
    
    return 0 if result.get("ok") else 1


# ============================================================
# Subcommand: doctor
# ============================================================
def build_doctor_parser(subparsers) -> None:
    build_config_parser(subparsers)
    parser = subparsers.add_parser(
        "doctor",
        help="Check ARC configuration and environment",
        description="Validate configuration, check dependencies, and diagnose common issues.",
    )
    parser.set_defaults(func=cmd_doctor)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Execute doctor subcommand."""
    _ensure_dotenv_loaded()
    from config_validator import print_health_check
    return print_health_check()


# ============================================================
# Subcommand: init
# ============================================================

# ============================================================
# Subcommand: config
# ============================================================
def build_config_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "config",
        help="Configure ARC interactively",
        description="Create or update .env file with core configuration.",
    )
    parser.set_defaults(func=cmd_config)


def cmd_config(args: argparse.Namespace) -> int:
    """Execute config subcommand."""
    from config_validator import interactive_config_setup
    return interactive_config_setup()


# ============================================================
# Main CLI entry
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc",
        description="ARC: Agentic Requirement Compiler",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="ARC 1.1.0",
    )
    
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="Available commands",
    )
    
    build_compile_parser(subparsers)
    build_doctor_parser(subparsers)
    
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    
    # Call subcommand handler
    if asyncio.iscoroutinefunction(args.func):
        exit_code = asyncio.run(args.func(args))
    else:
        exit_code = args.func(args)
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
