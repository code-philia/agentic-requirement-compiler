from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app", description="Template ARC CLI application.")
    parser.add_argument("--name", default="world", help="Name to greet.")
    parser.add_argument("--uppercase", action="store_true", help="Render the greeting in uppercase.")
    return parser


def format_greeting(name: str, *, uppercase: bool = False) -> str:
    message = f"Hello, {name}!"
    return message.upper() if uppercase else message


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(format_greeting(args.name, uppercase=args.uppercase))
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
