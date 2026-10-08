from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time
from dataclasses import dataclass

from app_type_handler import (
    get_run_instructions,
    list_app_types,
    normalize_app_type,
)
from core.cli import (
    cli_log,
    init_debug_logger,
    print_cli_banner,
    print_cli_startup,
    print_compilation_summary,
    stop_cli_spinner,
)
from core.config import set_web_port
from core.workflow import ARCWorkflowManager, load_project_metadata


@dataclass(slots=True)
class CompilationConfig:
    output_dir: str
    requirement_dir: str
    requirement_path: str
    user_requested_clear_all: bool = False
    app_type: str = "web"
    web_port: int = 3000
    resume_from_queue: bool = False
    retry_failed: bool = False
    retry_node_ids: list[str] | None = None
    rerun_tdd_node_id: str | None = None
    selected_test_ids: list[str] | None = None
    add_tests_node_id: str | None = None
    regenerate_tests_node_id: str | None = None
    regenerate_test_id: str | None = None
    test_intent: str | None = None
    sync_requirements: bool = False
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
        default=None,
        help=(
            f"Application type for a new workspace (choices: {', '.join(list_app_types())}); "
            "on --resume, defaults to the workspace's saved type"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Web server port (only for app-type=web; on --resume, defaults to the saved port)",
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
    parser.add_argument(
        "--rerun-tdd",
        metavar="NODE_ID",
        help="Run TDD only for selected registered tests of one leaf node (requires --resume)",
    )
    parser.add_argument(
        "--test",
        dest="selected_test_ids",
        action="append",
        metavar="TEST_ID",
        help="Registered test ID to include with --rerun-tdd; repeat for multiple tests",
    )
    parser.add_argument(
        "--add-tests",
        metavar="NODE_ID",
        help="Generate and append tests for an intent on one leaf node (requires --resume)",
    )
    parser.add_argument(
        "--regenerate-tests",
        metavar="NODE_ID",
        help="Regenerate one registered test on a leaf node (requires --resume and --test)",
    )
    parser.add_argument(
        "--intent",
        help="Required test intent text for --add-tests",
    )
    parser.add_argument(
        "--sync-requirements",
        action="store_true",
        help="Incrementally compile newly added or modified requirements (requires --resume)",
    )
    parser.set_defaults(func=cmd_compile)


def _resolve_workspace_settings(
    *,
    output_dir: str,
    resume_from_queue: bool,
    requested_app_type: str | None,
    requested_web_port: int | None,
) -> tuple[str, int]:
    """Resolve explicit CLI settings against persisted workspace metadata."""
    metadata = load_project_metadata(output_dir) if resume_from_queue else None
    saved_app_type = metadata.get("app_type") if metadata else None
    saved_web_port = metadata.get("web_port") if metadata else None

    if requested_app_type is not None:
        app_type = normalize_app_type(requested_app_type)
        if saved_app_type and app_type != saved_app_type:
            raise ValueError(
                f"--type {app_type!r} conflicts with saved workspace type {saved_app_type!r}. "
                "Create a new output directory to change application type."
            )
    else:
        app_type = saved_app_type or "web"

    if requested_web_port is not None:
        web_port = requested_web_port
        if saved_web_port is not None and app_type == "web" and web_port != saved_web_port:
            raise ValueError(
                f"--port {web_port} conflicts with saved workspace port {saved_web_port}. "
                "Omit --port to reuse the saved setting."
            )
    else:
        web_port = saved_web_port if saved_web_port is not None else 3000

    return app_type, web_port


async def cmd_compile(args: argparse.Namespace) -> int:
    """Execute compile subcommand."""
    _ensure_dotenv_loaded()
    
    # Validate mutual exclusivity
    if args.clean and args.resume:
        print("Error: --clean and --resume are mutually exclusive")
        return 2
    if (args.retry_failed or args.retry or args.rerun_tdd or args.add_tests or args.regenerate_tests or args.sync_requirements) and not args.resume:
        print("Error: interactive compile actions require --resume")
        return 2
    if args.retry_failed and args.retry:
        print("Error: --retry-failed and --retry are mutually exclusive")
        return 2
    if sum(bool(value) for value in (args.retry_failed, args.retry, args.rerun_tdd, args.add_tests, args.regenerate_tests, args.sync_requirements)) > 1:
        print("Error: only one interactive compile action may be requested at a time")
        return 2
    if args.rerun_tdd and not args.selected_test_ids:
        print("Error: --rerun-tdd requires at least one --test TEST_ID")
        return 2
    if args.regenerate_tests and len(args.selected_test_ids or []) != 1:
        print("Error: --regenerate-tests requires exactly one --test TEST_ID")
        return 2
    if args.selected_test_ids and not (args.rerun_tdd or args.regenerate_tests):
        print("Error: --test may only be used with --rerun-tdd or --regenerate-tests")
        return 2
    if (args.add_tests or args.regenerate_tests) and not str(args.intent or "").strip():
        print("Error: --add-tests and --regenerate-tests require a non-empty --intent")
        return 2
    if args.intent and not (args.add_tests or args.regenerate_tests):
        print("Error: --intent may only be used with --add-tests or --regenerate-tests")
        return 2
    
    # Normalize paths
    requirement_dir, requirement_path, _ = _locate_requirement_file(args.requirement_path)
    output_dir = os.path.abspath(args.output_dir)
    
    # Handle --clean
    if args.clean and os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    
    try:
        normalized_app_type, resolved_web_port = _resolve_workspace_settings(
            output_dir=output_dir,
            resume_from_queue=args.resume,
            requested_app_type=args.app_type,
            requested_web_port=args.port,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        return 2
    
    # Set web port
    set_web_port(resolved_web_port)
    
    # Model API mode
    model_api_mode = os.environ.get("ARC_OPENAI_API_MODE", "").strip() or None
    
    config = CompilationConfig(
        output_dir=output_dir,
        requirement_dir=requirement_dir,
        requirement_path=requirement_path,
        user_requested_clear_all=args.clean,
        app_type=normalized_app_type,
        web_port=resolved_web_port,
        resume_from_queue=args.resume,
        retry_failed=args.retry_failed,
        retry_node_ids=args.retry or None,
        rerun_tdd_node_id=args.rerun_tdd or None,
        selected_test_ids=args.selected_test_ids or None,
        add_tests_node_id=args.add_tests or None,
        regenerate_tests_node_id=args.regenerate_tests or None,
        regenerate_test_id=(args.selected_test_ids or [None])[0] if args.regenerate_tests else None,
        test_intent=args.intent or None,
        sync_requirements=args.sync_requirements,
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
            rerun_tdd_node_id=config.rerun_tdd_node_id,
            selected_test_ids=config.selected_test_ids,
            add_tests_node_id=config.add_tests_node_id,
            regenerate_tests_node_id=config.regenerate_tests_node_id,
            regenerate_test_id=config.regenerate_test_id,
            test_intent=config.test_intent,
            sync_requirements=config.sync_requirements,
        )
    finally:
        stop_cli_spinner()
    
    elapsed = time.time() - start_time
    print_compilation_summary(
        result,
        config.output_dir,
        elapsed,
        run_instructions=get_run_instructions(
            config.app_type,
            web_port=config.web_port,
        ),
    )
    
    # A completed compilation is a successful CLI operation even when one or
    # more requirement nodes/tests remain failed. The detailed result and
    # generated artifacts communicate requirement coverage; the process exit
    # code is reserved for invocation/configuration/runtime failures that
    # prevent ARC from completing its workflow.
    return 0


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
    from core.config import print_health_check
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
    from core.config import interactive_config_setup
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
        version="ARC 1.2.0",
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
    import sys
    argv = sys.argv[1:]
    if argv and argv[0] not in {"compile", "doctor", "config", "--version", "-h", "--help"}:
        argv = ["compile", *argv]
    args = parser.parse_args(argv)

    
    # Call subcommand handler
    if asyncio.iscoroutinefunction(args.func):
        exit_code = asyncio.run(args.func(args))
    else:
        exit_code = args.func(args)
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
