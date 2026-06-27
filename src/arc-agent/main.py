import argparse
import asyncio

from utils import print_cli_banner, run_cli_compilation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run ARC agent workflow directly from terminal (no websocket UI needed)."
    )
    parser.add_argument(
        "project_path",
        nargs="?",
        required=True,
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


def main() -> None:
    args = parse_args()

    print_cli_banner()
    
    asyncio.run(
        run_cli_compilation(
            project_path=args.project_path,
            requirement_path=args.requirement_path,
            clear_all=args.clear_all,
            app_type=args.app_type,
        )
    )


if __name__ == "__main__":
    main()
