"""The installed dream command."""

import argparse
import sys
from pathlib import Path

from dreaming import __version__
from dreaming.config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dream", description="Learn from recorded agent sessions")
    parser.add_argument("--version", action="version", version=f"dream {__version__}")
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command")
    probe = commands.add_parser("probe", help="Check backend capabilities and isolation")
    probe.add_argument("--dry-run", action="store_true")
    probe.add_argument("--backend", action="append")
    args = parser.parse_args(argv)
    try:
        if args.command == "probe":
            config = load_config(args.config)
            from dreaming.backends.probe import probe_config

            return probe_config(config, dry_run=args.dry_run, names=args.backend)
    except ConfigError as exc:
        print(f"dream: {exc}", file=sys.stderr)
        return 2
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
