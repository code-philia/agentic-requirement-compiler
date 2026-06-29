import argparse
import asyncio
import os
from dataclasses import dataclass

from agent_workflow import ARCWorkflowManager
from app_types import normalize_app_type, upsert_metadata
from utils import detect_requirement_path, set_web_port
from utils import cli_log, init_debug_logger, print_cli_banner, print_cli_startup, stop_cli_spinner


@dataclass(slots=True)
class CompilationConfig:
    project_path: str
    requirement_path: str
    clear_all: bool = False
    app_type: str = "web"
    web_port: int = 3301
    resume_from_queue: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ARC agent workflow from the command line.")
    parser.add_argument("project_path", help="Target project root path.")
    parser.add_argument(
        "--requirement-path",
        help="Requirement yaml path. Absolute path, or relative to project path.",
    )
    parser.add_argument(
        "--clear-all",
        action="store_true",
        help="Clear project workspace and recompile.",
    )
    parser.add_argument(
        "--app-type",
        choices=["web", "android"],
        default="web",
        help="Application type for stack metadata writing.",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=3301,
        help="Single backend port for web apps. The backend serves the built frontend on this port.",
    )
    return parser.parse_args()


def prepare_config(args: argparse.Namespace) -> CompilationConfig:
    normalized_project_path = os.path.abspath(args.project_path)
    if not os.path.isdir(normalized_project_path):
        raise FileNotFoundError(f"Project path does not exist: {normalized_project_path}")

    normalized_app_type = normalize_app_type(args.app_type)
    normalized_requirement_path = detect_requirement_path(normalized_project_path, args.requirement_path)
    if not os.path.exists(normalized_requirement_path):
        raise FileNotFoundError(f"Requirement file does not exist: {normalized_requirement_path}")

    web_port = int(args.web_port)
    if web_port < 1 or web_port > 65535:
        raise ValueError(f"Web port must be between 1 and 65535, got: {web_port}")

    set_web_port(web_port)
    queue_path = os.path.join(normalized_project_path, ".arc", "processing_queue.json")
    resume_from_queue = (not args.clear_all) and os.path.exists(queue_path)
    if not resume_from_queue:
        upsert_metadata(normalized_project_path, normalized_app_type)
    return CompilationConfig(
        project_path=normalized_project_path,
        requirement_path=normalized_requirement_path,
        clear_all=args.clear_all,
        app_type=normalized_app_type,
        web_port=web_port,
        resume_from_queue=resume_from_queue,
    )


async def run() -> None:
    config = prepare_config(parse_args())

    print_cli_banner()
    log_path = init_debug_logger(config.project_path, reset_existing=not config.resume_from_queue)
    print_cli_startup(
        project_path=config.project_path,
        requirement_path=config.requirement_path,
        app_type=config.app_type,
        clear_all=config.clear_all,
        log_path=log_path,
        web_port=config.web_port,
        resume_from_queue=config.resume_from_queue,
    )

    try:
        workflow_manager = ARCWorkflowManager(
            workspace_path=config.project_path,
            requirement_path=config.requirement_path,
            app_type=config.app_type,
            web_port=config.web_port,
            log_cb=cli_log,
        )
        await workflow_manager.start_compilation(
            clear_all=config.clear_all,
            resume_from_queue=config.resume_from_queue,
        )
    finally:
        stop_cli_spinner()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
