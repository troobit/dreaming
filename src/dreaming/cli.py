"""The `dream` command."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from dreaming import __version__, budget, config, plan, run, steps, store
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


def _usage(message: str) -> DreamingError:
    return DreamingError(message, USAGE_ERROR, "invalid_request")


def _selection(args: argparse.Namespace) -> dict:
    """Which sessions to read (1.3, 1.4, 2.8); empty means the default selection."""
    selection: dict = {}
    for key in ("since", "until"):
        value = getattr(args, key)
        if value is not None:
            try:
                datetime.fromisoformat(value)
            except ValueError as exc:
                raise _usage(f"--{key} {value!r} is not an ISO 8601 date") from exc
            selection[key] = value
    if args.project:
        selection["project"] = args.project
    if args.reread:
        selection["reread"] = True
    scope = {"project": args.repo, "workflow": args.workflow}
    scope = {key: value for key, value in scope.items() if value}
    if scope:
        selection["scope"] = scope
    return selection


def _overrides(args: argparse.Namespace) -> dict:
    """Per-run overrides of the budget, role backends and usage-limit fallback."""
    overrides: dict = {}
    limits = {
        "sessions": args.sessions,
        "tokens": args.tokens,
        "wall_clock_minutes": args.minutes,
    }
    limits = {key: value for key, value in limits.items() if value is not None}
    if limits:
        overrides["budget"] = limits
    roles = {}
    for choice in args.backend:
        role, _, names = choice.partition("=")
        if not role or not names:
            raise _usage(f"--backend {choice!r} must look like ROLE=NAME[,NAME...]")
        roles[role] = names.split(",")
    if roles:
        overrides["roles"] = roles
    if args.fallback_on_usage_limit:
        overrides["fallback_on_usage_limit"] = True
    return overrides


def _describe_dry_run(result: dict) -> str:
    limits = result["budget"]
    lines = ["Dry run: no model is called and nothing is changed."]
    if result["resumes"]:
        lines.append(
            f"A start would resume dream {result['resumes']} with its own selection."
        )
    lines.append(
        f"Budget: {limits['sessions']} sessions, {limits['tokens']} tokens, "
        f"{limits['wall_clock_minutes']} wall-clock minutes, "
        f"{limits['review_author_reserve']:.0%} reserved for review and authoring"
    )
    lines.append(f"Sessions ({len(result['sessions'])}):")
    for s in result["sessions"]:
        read = f"{s['entries']} entries"
        if s["from_entry"]:
            read += f" from entry {s['from_entry']}"
        lines.append(
            f"  {s['source']} {s['id']}  {s['project'] or '-'}  "
            f"{s['started_at'] or '-'}  {read}"
        )
    if result["skipped"]:
        lines.append(f"Skipped ({len(result['skipped'])}):")
        lines += [
            f"  {s['source']} {s['id']}: {s['reason']}" for s in result["skipped"]
        ]
    lines.append("Roles:")
    for role, backends in result["roles"].items():
        shown = []
        for b in backends:
            # Unprobed only means no cached probe for this version (1.7).
            if b["usable"] or b["status"] == "unprobed":
                shown.append(f"{b['backend']} ({b['status']})")
            else:
                shown.append(
                    f"{b['backend']} ({b['status']}, not usable: {b['reason']})"
                )
        lines.append(f"  {role}: {', '.join(shown)}")
    return "\n".join(lines)


def _run(args: argparse.Namespace) -> int:
    if args.dry_run:
        home = config.dreaming_home()
        result = run.dry_run(
            home,
            config.load_config(home),
            selection=_selection(args),
            overrides=_overrides(args),
        )
        print(json.dumps(result, indent=2) if args.json else _describe_dry_run(result))
        return 0
    home = config.ensure_home()
    cfg = config.load_config(home)
    started = run.start(
        home,
        cfg,
        selection=_selection(args),
        overrides=_overrides(args),
        requester=args.requester,
    )
    if started.resumed:
        print(f"resumed {started.run_id}")
        print(f"  selection: {json.dumps(started.selection)}")
        print(f"  overrides: {json.dumps(started.overrides)}")
    else:
        print(f"started {started.run_id}")
    sys.stdout.flush()
    return started.process.wait() if args.wait else 0


def _status(args: argparse.Namespace) -> int:
    home = config.dreaming_home()
    path = store.store_path(home)
    if not path.exists():
        if args.run:
            raise DreamingError(
                f"no record of dream {args.run}", USAGE_ERROR, "not_found"
            )
        print("no dream has run yet")
        return 0
    conn = store.open_store(path)
    try:
        row = run.find_run(conn, args.run)
        if args.plan:
            print(plan.write(home, conn, row["id"]))
            return 0
        print(f"dream {row['id']}: {row['status']}")
        print(f"  requested by {row['requester']}, started {row['started_at']}")
        if row["status"] == "waiting":
            print(f"  waiting until {row['wait_until']}: {row['wait_notice']}")
        if row["error"]:
            print(f"  stopped by: {row['error']}")
        counts: dict[str, int] = {}
        for step in steps.load(conn, row["id"]):
            counts[step.state] = counts.get(step.state, 0) + 1
        if counts:
            shown = ", ".join(f"{n} {state}" for state, n in counts.items())
            print(f"  steps: {shown}")
        limits = json.loads(row["budget"])
        used = budget.tokens_used(conn, row["id"])
        print(
            f"  tokens: {used} of {limits.get('tokens', '?')}; running "
            f"{row['active_seconds'] / 60:.1f} of "
            f"{limits.get('wall_clock_minutes', '?')} minutes"
        )
    finally:
        conn.close()
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

    dream = commands.add_parser(
        "run",
        help="start a dream in the background, or resume the interrupted one",
    )
    pick = dream.add_argument_group("selection")
    pick.add_argument("--since", metavar="DATE", help="sessions started at or after")
    pick.add_argument("--until", metavar="DATE", help="sessions started before")
    pick.add_argument("--project", metavar="PATH", help="sessions in this project")
    pick.add_argument(
        "--reread", action="store_true", help="read again under current lenses"
    )
    pick.add_argument("--repo", metavar="PATH", help="scope the dream to a repository")
    pick.add_argument(
        "--workflow", metavar="NAME", help="scope the dream to a workflow"
    )
    limits = dream.add_argument_group("per-run overrides")
    limits.add_argument("--sessions", type=int, metavar="N")
    limits.add_argument("--tokens", type=int, metavar="N")
    limits.add_argument("--minutes", type=int, metavar="N", help="wall-clock limit")
    limits.add_argument(
        "--backend",
        action="append",
        default=[],
        metavar="ROLE=NAME[,NAME...]",
        help="a role's backends in fallback order",
    )
    limits.add_argument(
        "--fallback-on-usage-limit",
        action="store_true",
        help="move to the next backend at a usage limit instead of waiting",
    )
    dream.add_argument(
        "--requester", default="user", help="who asked for the dream (default user)"
    )
    dream.add_argument(
        "--wait", action="store_true", help="wait for the dream and exit with its code"
    )
    dream.add_argument(
        "--dry-run",
        action="store_true",
        help="list the sessions, backends and budget a start would use; "
        "call no model and change nothing",
    )
    dream.add_argument("--json", action="store_true", help="print the dry run as JSON")
    dream.set_defaults(handler=_run)

    status = commands.add_parser("status", help="show a dream's state")
    status.add_argument("run", nargs="?", metavar="RUN", help="default: the latest")
    status.add_argument(
        "--plan",
        action="store_true",
        help="write the steps as a rune task file and print its path",
    )
    status.set_defaults(handler=_status)
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
