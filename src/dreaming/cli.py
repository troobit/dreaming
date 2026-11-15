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
            if isinstance(request.get("op"), str) and request["op"].startswith("dream."):
                result = handle_request(None, config, request)
            else:
                writable = request.get("op") in (
                    "knowledge.feedback",
                    "knowledge.import",
                    "session.register",
                    "session.forget",
                    "session.restore",
                )
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
    return 0 if result["ok"] else 4 if result.get("code") == "already_running" else 2


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
    start = commands.add_parser("start", help="Start or resume a detached dream")
    for name in ("project", "workflow", "domain", "since", "until"):
        start.add_argument("--" + name)
    start.add_argument("--reread", action="store_true")
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--requester", default="user")
    start.add_argument("--backends", type=json.loads)
    start.add_argument("--no-browser", action="store_true")
    start.add_argument("--fallback-on-usage-limit", action="store_true", default=None)
    for name in ("max-tokens", "max-sessions", "max-seconds"):
        start.add_argument("--" + name, type=int)
    for name in ("resume", "discard"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        if name == "resume":
            command.add_argument("--backends", type=json.loads, default={})
    for name in ("forget", "restore"):
        command = commands.add_parser(name)
        command.add_argument("session_id")
        command.add_argument("--source", required=True)
    register = commands.add_parser("register")
    for name in (
        "tool",
        "run-ref",
        "source",
        "session-id",
        "correlation-id",
        "workflow",
        "project",
        "document-path",
    ):
        register.add_argument("--" + name, required=name in {"tool", "run-ref"})
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
        if args.command in {"start", "resume", "discard", "forget", "restore", "register"}:
            from dreaming.api import handle_request
            from dreaming.store import Store

            fields = {
                k: v
                for k, v in vars(args).items()
                if k not in {"config", "command"} and v is not None
            }
            if args.command == "start":
                fields["budget"] = {
                    k: fields.pop(k)
                    for k in ("max_tokens", "max_sessions", "max_seconds")
                    if k in fields
                }
                if not fields["budget"]:
                    fields.pop("budget")
                if fields.pop("no_browser"):
                    fields["open_browser"] = False
                for flag in ("reread", "dry_run"):
                    if not fields.get(flag):
                        fields.pop(flag, None)
            op = (
                "session." if args.command in {"forget", "restore", "register"} else "dream."
            ) + args.command
            request = {"dreaming_api": 1, "op": op, **fields}
            config = load_config(args.config)
            if op.startswith("session."):
                with Store(home_path()) as store:
                    result = handle_request(store, config, request)
            else:
                result = handle_request(None, config, request)
            print(json.dumps(result))
            return 0 if result["ok"] else 4 if result.get("code") == "already_running" else 2
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
