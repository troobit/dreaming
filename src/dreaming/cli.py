"""The `dream` command."""

from __future__ import annotations

import argparse
import json
import sys

from dreaming import __version__, config
from dreaming.backends import probe
from dreaming.errors import USAGE_ERROR, DreamingError


def _config(args: argparse.Namespace) -> int:
    print(json.dumps(config.load_config(), indent=2))
    return 0


def _probe(args: argparse.Namespace) -> int:
    cfg = config.load_config()
    names = args.backends or list(cfg["backends"])
    unknown = [name for name in names if name not in cfg["backends"]]
    if unknown:
        raise DreamingError(
            f"unknown backend {unknown[0]!r}", USAGE_ERROR, "invalid_config"
        )
    if args.cached:
        home = config.dreaming_home()
        results = [probe.cached_status(home, cfg, name) for name in names]
    else:
        home = config.ensure_home()
        results = [probe.probe_backend(home, cfg, name) for name in names]
    if args.json:
        print(json.dumps([result.to_dict() for result in results], indent=2))
    else:
        for result in results:
            print(probe.describe(result))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dream",
        description="Turns evidence from past agent sessions into retained knowledge.",
    )
    parser.add_argument("--version", action="version", version=f"dream {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    show = commands.add_parser("config", help="print the effective configuration")
    show.set_defaults(handler=_config)

    check = commands.add_parser(
        "probe", help="check each backend's isolation, tools and structured output"
    )
    check.add_argument("backends", nargs="*", metavar="BACKEND")
    check.add_argument(
        "--cached",
        action="store_true",
        help="show cached results and versions only, with no model call",
    )
    check.add_argument("--json", action="store_true", help="print JSON")
    check.set_defaults(handler=_probe)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        if args.command is None:
            parser.print_help()
            return 0
        return args.handler(args)
    except DreamingError as exc:
        print(f"dream: {exc.message}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
