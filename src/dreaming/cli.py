"""The installed dream command."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from dreaming import __version__
from dreaming.config import ConfigError, home_path, load_config
from dreaming.sessions import SessionError
from dreaming.store import StoreError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dream", description="Learn from recorded agent sessions")
    parser.add_argument("--version", action="version", version=f"dream {__version__}")
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command")
    probe = commands.add_parser("probe", help="Check backend capabilities and isolation")
    probe.add_argument("--dry-run", action="store_true")
    probe.add_argument("--backend", action="append")
    run = commands.add_parser("run", help="Preview a dream without calling models")
    run.add_argument("--dry-run", action="store_true", required=True)
    run.add_argument("--project")
    run.add_argument("--workflow")
    run.add_argument("--domain")
    run.add_argument("--since")
    run.add_argument("--until")
    run.add_argument("--reread", action="store_true")
    status = commands.add_parser("status", help="Inspect a recorded run")
    status.add_argument("run_id", nargs="?")
    status.add_argument("--plan", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "probe":
            config = load_config(args.config)
            from dreaming.backends.probe import probe_config

            return probe_config(config, dry_run=args.dry_run, names=args.backend)
        if args.command == "run":
            from dreaming.plan import dry_run
            from dreaming.sessions import SessionScope

            result = dry_run(
                load_config(args.config),
                home_path(),
                scope=SessionScope(args.project, args.workflow, args.domain),
                since=args.since,
                until=args.until,
                reread=args.reread,
            )
            print(json.dumps(result))
            return 0
        if args.command == "status":
            from dreaming.plan import render_plan
            from dreaming.run import inspect_run
            from dreaming.store import Store

            record = inspect_run(home_path(), args.run_id)
            if record and args.plan:
                with Store.readonly(home_path()) as store:
                    record["plan_path"] = str(render_plan(store, record["id"]))
            print(json.dumps(record or {"status": "not_found"}))
            return 0
    except (ConfigError, SessionError, StoreError, sqlite3.Error, OSError) as exc:
        print(f"dream: {exc}", file=sys.stderr)
        return 2
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
