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


def _api_main(config_path):
    from dreaming.api import handle_request
    from dreaming.store import MigrationBlocked, Store

    request = None
    try:
        request = json.load(sys.stdin)
        if (
            not isinstance(request, dict)
            or type(request.get("dreaming_api")) is not int
            or request["dreaming_api"] != 1
        ):
            result = handle_request(None, None, request)
        else:
            config = load_config(config_path)
            writable = request.get("op") in ("knowledge.feedback", "knowledge.import")
            with Store(home_path()) if writable else Store.readonly(home_path()) as store:
                result = handle_request(store, config, request)
    except (ValueError, StoreError, sqlite3.Error, OSError) as exc:
        code = "invalid_request"
        if isinstance(exc, MigrationBlocked):
            code = "migration_blocked"
        elif isinstance(exc, ConfigError):
            code = "invalid_config"
        elif isinstance(exc, (StoreError, sqlite3.Error, OSError)):
            code = "store_error"
        result = {
            "dreaming_api": 1,
            "op": request.get("op") if isinstance(request, dict) else None,
            "ok": False,
            "code": code,
            "message": str(exc),
        }
    print(json.dumps(result))
    return 0 if result["ok"] else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dream", description="Learn from recorded agent sessions")
    parser.add_argument("--version", action="version", version=f"dream {__version__}")
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("api", help="Read one versioned knowledge request as JSON from stdin")
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
    trial = commands.add_parser("trial", help="Measure a reader backend without store writes")
    trials = trial.add_subparsers(dest="trial_role", required=True)
    reader = trials.add_parser("reader")
    reader.add_argument("--backend", required=True)
    reader.add_argument("--sessions", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        if args.command == "api":
            return _api_main(args.config)
        if args.command == "probe":
            config = load_config(args.config)
            from dreaming.backends.probe import probe_config

            return probe_config(config, dry_run=args.dry_run, names=args.backend)
        if args.command == "trial":
            from dreaming.trial import run_trial

            result = run_trial(
                load_config(args.config),
                home_path(),
                backend_name=args.backend,
                max_sessions=args.sessions,
            )
            print(json.dumps(result))
            return 0
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
