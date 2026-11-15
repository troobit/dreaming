"""The `dream` command."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from dreaming import (
    __version__,
    api,
    config,
    knowledge,
    observe,
    plan,
    roles,
    run,
    sessions,
    store,
)
from dreaming.backends import probe
from dreaming.errors import FAILURE, USAGE_ERROR, DreamingError


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


def _resume(args: argparse.Namespace) -> int:
    home = config.ensure_home()
    started = run.start(
        home,
        config.load_config(home),
        overrides=_overrides(args),
        resume=True,
        run_id=args.run,
    )
    print(f"resumed {started.run_id}")
    print(f"  selection: {json.dumps(started.selection)}")
    print(f"  overrides: {json.dumps(started.overrides)}")
    sys.stdout.flush()
    return started.process.wait() if args.wait else 0


def _open_store(run_id: str | None):
    """The store, or None when no dream has run yet."""
    path = store.store_path(config.dreaming_home())
    if path.exists():
        return store.open_store(path)
    if run_id:
        raise DreamingError(f"no record of dream {run_id}", USAGE_ERROR, "not_found")
    return None


def _status(args: argparse.Namespace) -> int:
    conn = _open_store(args.run)
    if conn is None:
        print("no dream has run yet")
        return 0
    try:
        if args.plan:
            row = run.find_run(conn, args.run)
            print(plan.write(config.dreaming_home(), conn, row["id"]))
            return 0
        state = run.status(conn, args.run)
    finally:
        conn.close()
    print(f"dream {state['run_id']}: {state['status']}")
    print(f"  requested by {state['requester']}, started {state['started_at']}")
    waiting = state["waiting"]
    if waiting:
        print(f"  waiting until {waiting['until']}")
        if waiting.get("message"):
            print(f"  {waiting['message']}")
    if state["error"]:
        print(f"  stopped by: {state['error']}")
    if state["steps"]:
        shown = ", ".join(f"{n} {s}" for s, n in state["steps"].items())
        print(f"  steps: {shown}")
    limits = state["budget"]
    print(
        f"  tokens: {state['tokens_used']} of {limits.get('tokens', '?')}; running "
        f"{state['active_seconds'] / 60:.1f} of "
        f"{limits.get('wall_clock_minutes', '?')} minutes"
    )
    return 0


def _discard(args: argparse.Namespace) -> int:
    result = run.discard(config.ensure_home(), args.run)
    print(f"discarded {result['run_id']}")
    for removed in result["branches"]:
        print(f"  deleted {removed['branch']} in {removed['repo']}")
    return 0


def _report(args: argparse.Namespace) -> int:
    conn = _open_store(args.run)
    if conn is None:
        raise DreamingError("no dream has run yet", USAGE_ERROR, "not_found")
    try:
        result = api.dream_report(conn, {}, {"run_id": args.run}, None)
    finally:
        conn.close()
    print(result["report"])
    return 0


def _forget(args: argparse.Namespace) -> int:
    handler = knowledge.forget_session if args.forget else knowledge.restore_session
    conn = store.open_store(store.store_path(config.ensure_home()))
    try:
        result = handler(conn, args.session, args.source)
    finally:
        conn.close()
    verb = "forgot" if args.forget else "restored"
    print(f"{verb} {args.session}" if result["changed"] else "nothing changed")
    if result["reassess"]:
        print(f"  queued for reassessment: {', '.join(result['reassess'])}")
    return 0


def _describe_trial(result: dict) -> str:
    def share(part: int, whole: int, rate: float | None) -> str:
        return f"{part} of {whole}" + (f" ({rate:.0%})" if rate is not None else "")

    answered = result["calls"] - result["failed_calls"]
    estimated = " (estimated)" if result["usage_estimated"] else ""
    lines = [
        f"Reader trial on {result['backend']}: {result['sessions']} sessions, "
        f"{result['calls']} calls, {result['failed_calls']} failed",
        "  schema-valid outputs: "
        + share(result["schema_valid"], answered, result["schema_valid_rate"]),
        "  quotes matching their entry: "
        + share(
            result["quote_matches"], result["observations"], result["quote_match_rate"]
        ),
        f"  observations passing every check: {result['accepted']}",
        f"  tokens: {result['tokens_in']} in, {result['tokens_out']} out{estimated}",
    ]
    lines += [f"  error: {error}" for error in result["errors"]]
    return "\n".join(lines)


def _trial(args: argparse.Namespace) -> int:
    """Run the reader on recorded sessions with one backend; nothing is stored
    (design, Risks and Assumptions)."""
    cfg = config.load_config()
    if args.backend not in cfg["backends"]:
        raise _usage(f"unknown backend {args.backend!r}")
    if args.sessions < 1:
        raise _usage("--sessions must be at least 1")
    cfg = config.apply_overrides(
        cfg,
        {"roles": {"reader": [args.backend]}, "budget": {"sessions": args.sessions}},
    )
    home = config.ensure_home()
    trial_id = run.new_run_id(datetime.now(UTC)).replace("dream-", "dream-trial-", 1)
    if args.instructions:
        root = Path(args.instructions).expanduser()
    else:
        root = config.write_snapshot(home, trial_id, cfg) / "instructions"
    instructions = roles.load(root, cfg)
    result = probe.cached_status(home, cfg, args.backend)
    if not result.usable:
        result = probe.probe_backend(home, cfg, args.backend)
    backend = probe.adapter(home, cfg, trial_id, result)
    if backend is None:
        reason = result.reason or "; ".join(result.problems())
        raise DreamingError(
            f"backend {args.backend} is not usable: {reason}",
            FAILURE,
            "backend_unavailable",
        )
    selection = _selection(args)
    selection["reread"] = True
    conn = store.memory_copy(store.store_path(home))
    try:
        chosen = sessions.select_sessions(
            conn,
            sessions.Apsis.from_config(cfg, home),
            cfg,
            sessions.SelectionRequest.from_dict(selection),
            home=home,
        )
    finally:
        conn.close()
    report = observe.trial_reader(chosen.sessions, backend, instructions, cfg)
    print(json.dumps(report, indent=2) if args.json else _describe_trial(report))
    return 0


def _api(args: argparse.Namespace) -> int:
    return api.main(sys.stdin, sys.stdout)


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

    resume = commands.add_parser(
        "resume",
        help="resume the interrupted dream; only backend choices may change",
    )
    resume.add_argument(
        "run", nargs="?", metavar="RUN", help="default: the interrupted one"
    )
    resume.add_argument(
        "--backend",
        action="append",
        default=[],
        metavar="ROLE=NAME[,NAME...]",
        help="a role's backends in fallback order",
    )
    resume.add_argument(
        "--wait", action="store_true", help="wait for the dream and exit with its code"
    )
    resume.set_defaults(
        handler=_resume,
        sessions=None,
        tokens=None,
        minutes=None,
        fallback_on_usage_limit=False,
    )

    status = commands.add_parser("status", help="show a dream's state")
    status.add_argument("run", nargs="?", metavar="RUN", help="default: the latest")
    status.add_argument(
        "--plan",
        action="store_true",
        help="write the steps as a rune task file and print its path",
    )
    status.set_defaults(handler=_status)

    discard = commands.add_parser(
        "discard",
        help="discard the interrupted dream: delete its unmerged branches and "
        "the records it wrote",
    )
    discard.add_argument(
        "run", nargs="?", metavar="RUN", help="default: the interrupted one"
    )
    discard.set_defaults(handler=_discard)

    report = commands.add_parser("report", help="print the path of a dream's report")
    report.add_argument("run", nargs="?", metavar="RUN", help="default: the latest")
    report.set_defaults(handler=_report)

    for name, forget, text in (
        ("forget", True, "stop counting a session and every copy of it (2.11)"),
        ("restore", False, "count a forgotten session again"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("session", metavar="SESSION")
        command.add_argument(
            "--source", help="the session's source, when the id is ambiguous"
        )
        command.set_defaults(handler=_forget, forget=forget)

    trial = commands.add_parser(
        "trial", help="try a role on recorded sessions without storing anything"
    )
    trials = trial.add_subparsers(dest="role", metavar="ROLE", required=True)
    reader = trials.add_parser(
        "reader",
        help="measure a backend's schema-valid and quote-match rates as a reader",
    )
    reader.add_argument("--backend", required=True, metavar="NAME")
    reader.add_argument(
        "--sessions", type=int, default=10, metavar="N", help="default 10"
    )
    reader.add_argument("--since", metavar="DATE", help="sessions started at or after")
    reader.add_argument("--until", metavar="DATE", help="sessions started before")
    reader.add_argument("--project", metavar="PATH", help="sessions in this project")
    reader.add_argument(
        "--instructions",
        metavar="DIR",
        help="an instruction set to use instead of the configured default branch",
    )
    reader.add_argument("--json", action="store_true", help="print JSON")
    reader.set_defaults(handler=_trial, reread=False, repo=None, workflow=None)

    machine = commands.add_parser(
        "api",
        help='answer one JSON request on stdin, {"dreaming_api": 1, "op": ...}, '
        "with one JSON response on stdout",
    )
    machine.set_defaults(handler=_api)
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
