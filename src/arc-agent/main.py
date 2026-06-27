import argparse
import asyncio

from compilation_service import prepare_compilation, start_compilation
from utils import cli_log, init_debug_logger, print_cli_banner, print_cli_startup, stop_cli_spinner


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
    return parser.parse_args()


async def run() -> None:
    args = parse_args()
    config = prepare_compilation(
        project_path=args.project_path,
        requirement_path=args.requirement_path,
        clear_all=args.clear_all,
        app_type=args.app_type,
    )

    print_cli_banner()
    log_path = init_debug_logger(config.project_path)
    print_cli_startup(
        project_path=config.project_path,
        requirement_path=config.requirement_path,
        app_type=config.app_type,
        clear_all=config.clear_all,
        log_path=log_path,
    )

    try:
        await start_compilation(config, cli_log)
    finally:
        stop_cli_spinner()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
