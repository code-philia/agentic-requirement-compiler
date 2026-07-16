from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import time
from dataclasses import dataclass

from app_type_handler import normalize_app_type
from core.utils import cli_log, init_debug_logger, print_cli_banner, print_cli_startup, set_web_port, stop_cli_spinner
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


def _get_repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _build_default_output_dir() -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(_get_repo_root(), "workspace", f"run-{timestamp}")


def _resolve_requirement_dir(path: str) -> str:
    normalized = os.path.abspath(path)
    if not os.path.isdir(normalized):
        raise FileNotFoundError(f"Requirement directory does not exist: {normalized}")
    requirement_file = os.path.join(normalized, "requirements.yaml")
    if not os.path.isfile(requirement_file):
        raise FileNotFoundError(f"Requirement directory must contain requirements.yaml: {normalized}")
    return normalized


def _reset_directory(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)


def _copy_requirement_dir_contents(requirement_dir: str, output_dir: str) -> None:
    target_requirements_dir = os.path.join(output_dir, "requirements")
    os.makedirs(target_requirements_dir, exist_ok=True)
    for entry in os.listdir(requirement_dir):
        src = os.path.join(requirement_dir, entry)
        dst = os.path.join(target_requirements_dir, entry)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ARC agent workflow from the command line.")
    parser.add_argument(
        "requirement_path",
        help="Requirement directory containing requirements.yaml and optional reference/ assets. Its contents will be copied into output-dir/requirements/ before compilation.",
    )
    parser.add_argument(
        "--output-dir",
        help="Output workspace directory. Defaults to <repo_root>/workspace/run-<timestamp>.",
    )
    parser.add_argument(
        "--clear-all",
        action="store_true",
        help="Reset the output directory before copying the requirement directory and recompiling.",
    )
    parser.add_argument(
        "--app-type",
        choices=["web", "android"],
        default="web",
        help="Application type for runtime stack context.",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=3000,
        help="Single backend port for web apps. The backend serves the built frontend on this port.",
    )
    return parser.parse_args()


def prepare_config(args: argparse.Namespace) -> CompilationConfig:
    normalized_output_dir = os.path.abspath(args.output_dir) if args.output_dir else _build_default_output_dir()
    normalized_requirement_dir = _resolve_requirement_dir(args.requirement_path)
    normalized_app_type = normalize_app_type(args.app_type)

    web_port = int(args.web_port)
    if web_port < 1 or web_port > 65535:
        raise ValueError(f"Web port must be between 1 and 65535, got: {web_port}")

    set_web_port(web_port)
    queue_path = os.path.join(normalized_output_dir, ".arc", "processing_queue.json")
    resume_from_queue = (not args.clear_all) and os.path.exists(queue_path)

    if not resume_from_queue:
        _reset_directory(normalized_output_dir)
        _copy_requirement_dir_contents(normalized_requirement_dir, normalized_output_dir)

    normalized_requirement_path = os.path.join(normalized_output_dir, "requirements", "requirements.yaml")
    if not os.path.isfile(normalized_requirement_path):
        raise FileNotFoundError(
            f"Copied requirement workspace is missing requirements.yaml: {normalized_requirement_path}"
        )

    return CompilationConfig(
        output_dir=normalized_output_dir,
        requirement_dir=normalized_requirement_dir,
        requirement_path=normalized_requirement_path,
        user_requested_clear_all=args.clear_all,
        app_type=normalized_app_type,
        web_port=web_port,
        resume_from_queue=resume_from_queue,
    )


async def run() -> None:
    config = prepare_config(parse_args())
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
    )
    try:
        workflow_manager = ARCWorkflowManager(
            workspace_path=config.output_dir,
            requirement_path=config.requirement_path,
            app_type=config.app_type,
            web_port=config.web_port,
            log_cb=cli_log,
        )
        await workflow_manager.start_compilation(
            clear_all=False,
            resume_from_queue=config.resume_from_queue,
        )
    finally:
        stop_cli_spinner()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
