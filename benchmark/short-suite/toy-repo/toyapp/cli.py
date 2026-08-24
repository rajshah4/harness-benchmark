from __future__ import annotations

import argparse


def format_greeting(name: str) -> str:
    return f"Hello, {name}."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="toyapp")
    parser.add_argument("--name", default="world", help="Name to greet.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    print(format_greeting(args.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
