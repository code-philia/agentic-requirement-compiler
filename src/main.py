from __future__ import annotations

import argparse
import json
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

from core.cli import (
    cli_log,
    init_debug_logger,
    print_cli_banner,
    print_cli_startup,
    print_compilation_summary,
)
from core.workflow import ARCWorkflowManager
from compiler.checkpoints import START_FROM, prepare_restart, install_restart_dependencies
from compiler.git_history import GitStageError


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


def _locate_requirement_file(input_path: str) -> str:
    """
    Locate requirements.yaml given an input path.
    Returns the absolute requirements file path.
    """
    abs_input = os.path.abspath(input_path)

    if os.path.isfile(abs_input):
        if not abs_input.endswith((".yaml", ".yml")):
            raise ValueError(f"Input file must be .yaml or .yml: {abs_input}")
        return abs_input

    if os.path.isdir(abs_input):
        # Input directory should directly contain requirements.yaml
        candidates = ["requirements.yaml", "requirements.yml"]
        for candidate in candidates:
            candidate_path = os.path.join(abs_input, candidate)
            if os.path.isfile(candidate_path):
                return candidate_path
        raise FileNotFoundError(f"No requirements.yaml found in {abs_input}")

    raise FileNotFoundError(f"Input path not found: {abs_input}")


# ============================================================
# Subcommand: compile
# ============================================================
def build_compile_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "compile",
        help="Compile requirements into a working application",
        description="Compile a requirement tree through design and lowering, optionally starting from a Git checkpoint.",
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
        "--port",
        type=int,
        default=None,
        help="Generated Web server port (default: ARC_WEB_PORT, then 3000)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove existing output directory before compilation",
    )
    parser.add_argument("--start-from", choices=START_FROM, default="zero",
                        help="Resume after a completed stage in an isolated Git worktree")
    parser.add_argument("--checkpoint-ref", help="Exact checkpoint commit/tag; default: latest matching checkpoint")
    parser.add_argument("--restart-output", help="New worktree directory; default: sibling of the source project")
    parser.add_argument("--resume", action="store_true",
                        help="Continue only unstarted TDD requirements in the existing output project")
    parser.set_defaults(func=cmd_compile)


async def cmd_compile(args: argparse.Namespace) -> int:
    """Execute compile subcommand."""
    _ensure_dotenv_loaded()
    web_port = _resolve_web_port(args.port)
    
    # Normalize paths
    requirement_path = _locate_requirement_file(args.requirement_path)
    output_dir = os.path.abspath(args.output_dir)
    if args.resume:
        if args.start_from != "zero" or args.clean or args.checkpoint_ref or args.restart_output:
            raise ValueError("--resume cannot be combined with --start-from, --clean, --checkpoint-ref or --restart-output")
        try:
            snapshot = json.loads((Path(output_dir) / ".arc/checkpoints/current.json").read_text(encoding="utf-8"))
            if not isinstance(snapshot, dict) or snapshot.get("stage") != "lowered":
                raise ValueError("--resume requires a project that completed lowering")
            saved_port = int(snapshot["web_port"])
            if args.port is not None and web_port != saved_port:
                raise ValueError(f"--resume requires --port {saved_port}")
            web_port = saved_port
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"Resume error: {exc}", file=sys.stderr)
            return 1
    
    if args.start_from == "zero" and (args.checkpoint_ref or args.restart_output):
        raise ValueError("--checkpoint-ref/--restart-output require a non-zero --start-from")
    if args.start_from != "zero" and args.clean:
        raise ValueError("--clean cannot be combined with a restart")
    restart_commit = None
    if args.start_from != "zero":
        try:
            restarted, checkpoint_port, restart_commit = prepare_restart(
                Path(output_dir), args.start_from, args.checkpoint_ref,
                Path(args.restart_output) if args.restart_output else None,
                requirement_path=Path(requirement_path), requested_port=web_port if args.port is not None else None,
            )
            output_dir = str(restarted)
            if args.port is not None and web_port != checkpoint_port:
                raise GitStageError(f"Checkpoint requires --port {checkpoint_port}; worktree retained at {output_dir}")
            web_port = checkpoint_port
        except GitStageError as exc:
            print(f"Checkpoint error: {exc}", file=sys.stderr)
            return 1

    # Handle --clean
    if args.clean and os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    
    # Print banner and startup info
    print_cli_banner()
    log_path = init_debug_logger(
        output_dir,
        reset_existing=not args.resume,
    )
    print_cli_startup(
        project_path=output_dir,
        requirement_path=requirement_path,
        clear_all=args.clean,
        log_path=log_path,
        web_port=web_port,
    )
    
    if restart_commit:
        print(f"Starting from {args.start_from} at {restart_commit}; worktree: {output_dir}")
        print("Installing locked dependencies in the restart worktree (npm ci)...")
        try:
            await asyncio.to_thread(install_restart_dependencies, Path(output_dir))
        except GitStageError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    # Run compilation
    start_time = time.time()
    workflow_manager = ARCWorkflowManager(
        workspace_path=output_dir,
        requirement_path=requirement_path,
        web_port=web_port,
        log_cb=cli_log,
        start_from=args.start_from,
        resume=args.resume,
    )
    result = await workflow_manager.start_compilation()
    
    elapsed = time.time() - start_time
    print_compilation_summary(result, output_dir, elapsed)
    
    return 0 if result.get("ok") else 1


def _resolve_web_port(cli_port: int | None) -> int:
    """Resolve one port for generated deployment, frontend, and E2E targets."""

    raw = os.environ.get("ARC_WEB_PORT", "").strip()
    value = int(cli_port) if cli_port is not None else (int(raw) if raw else 3000)
    if not 1 <= value <= 65535:
        raise ValueError("ARC_WEB_PORT/--port must be between 1 and 65535")
    return value


# ============================================================
# Subcommand: doctor
# ============================================================
def build_doctor_parser(subparsers) -> None:
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
    build_config_parser(subparsers)
    build_doctor_parser(subparsers)
    
    return parser


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
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
