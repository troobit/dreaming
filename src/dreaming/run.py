"""Running a dream: the run record, the lock, start and resume, and the step loop.

`start` runs in the invoking process. It takes the lock, records the run and
spawns a detached child in a new session that inherits the lock descriptor, so
the lock is held without a gap until the child exits or dies (design, Lock).
The child (`python -m dreaming.run <id> --lock-fd <fd>`) runs `Runner.execute`
and exits with the run's exit code.

The loop takes the next ready step, lets its phase make its calls through a
`Context`, and applies the result in one transaction with the step's follow-up
steps. Limits and stop requests are checked between steps and before each call,
never during one.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from dreaming import config, sessions, steps, store, versions
from dreaming.backends import (
    Backend,
    BackendError,
    CallResult,
    Dispatcher,
    InputFailed,
    SchemaError,
    estimate_tokens,
    probe,
)
from dreaming.budget import Budget, LimitReached, SystemClock, WallClock
from dreaming.errors import (
    FAILURE,
    PARTIAL,
    SKIPPED,
    SUCCESS,
    USAGE_ERROR,
    DreamingError,
    Partial,
)
from dreaming.schema import validate
from dreaming.steps import NewStep, Step

LOCK_NAME = "lock"
# Isolated (`-I`), so a checkout in the working directory cannot shadow the
# installed version.
CHILD = (sys.executable, "-I", "-m", "dreaming.run")
WAIT_POLL_SECONDS = 10.0

# `budget_exhausted` is the planned stop, so it exits 0 (Decision 16). An
# interrupted run, including one stopped by an error, can be resumed.
EXIT_CODES = {
    "complete": SUCCESS,
    "budget_exhausted": SUCCESS,
    "partial": PARTIAL,
    "interrupted": FAILURE,
}


def exit_code(status: str) -> int:
    return EXIT_CODES.get(status, FAILURE)


def log(message: str) -> None:
    print(message, flush=True)


# The run record (7.5)


def new_run_id(now: datetime) -> str:
    """`dream-…`: the prefix marks the run's LM Studio thread ids as the dream's own."""
    return f"dream-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def create_run(
    conn: sqlite3.Connection,
    run_id: str,
    cfg: dict,
    *,
    status: str = "running",
    requester: str = "user",
    revision: str | None = None,
    selection: dict | None = None,
    overrides: dict | None = None,
    snapshot_path: str | None = None,
    process_group: int | None = None,
    started_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO runs (id, requester, status, revision, started_at, process_group,"
        " selection, overrides, budget, snapshot_path)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            requester,
            status,
            revision or versions.revision(),
            started_at or SystemClock().now().isoformat(timespec="seconds"),
            process_group,
            json.dumps(selection or {}),
            json.dumps(overrides or {}),
            json.dumps(cfg["budget"]),
            snapshot_path,
        ),
    )


def find_run(conn: sqlite3.Connection, run_id: str | None = None) -> sqlite3.Row:
    """The named run, or the latest one."""
    if run_id is None:
        row = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
    else:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
    found = row.fetchone()
    if found is None:
        what = f"dream {run_id}" if run_id else "any dream"
        raise DreamingError(f"no record of {what}", USAGE_ERROR, "not_found")
    return found


def run_config(row: Mapping) -> dict:
    """The run's snapshot configuration with its per-run overrides (7.3)."""
    if not row["snapshot_path"]:
        raise DreamingError(f"dream {row['id']} has no configuration snapshot")
    snapshot = json.loads((Path(row["snapshot_path"]) / "config.json").read_text())
    return config.apply_overrides(snapshot, json.loads(row["overrides"]))


# The lock


def acquire_lock(home: Path) -> int | None:
    """The lock descriptor, or None when another process holds the lock.

    The lock file is created empty and never written or deleted; the kernel
    releases the lock when the last descriptor for it closes, including when
    its process dies.
    """
    fd = os.open(home / LOCK_NAME, os.O_RDONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def kill_survivors(group: int | None) -> None:
    """Kill what is left in a dead run's process group.

    Called only with the lock held, so the run's own process, the group leader,
    is gone. A live process with the group's id has reused the id and is left
    alone.
    """
    if not group:
        return
    try:
        os.kill(group, 0)
    except ProcessLookupError:
        pass
    except PermissionError:
        return
    else:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, signal.SIGKILL)


# Phases and the step loop


@dataclass(frozen=True)
class Outcome:
    """What applying a step's result did; follow-up steps commit with it."""

    state: str = "done"  # done | failed | skipped
    reason: str | None = None
    follow_up: Sequence[NewStep] = ()


class Phase(Protocol):
    def prepare(self, ctx: Context, step: Step) -> Any:
        """Gather the step's inputs and make its model calls through `ctx`."""

    def apply(self, conn: sqlite3.Connection, step: Step, prepared: Any) -> Outcome:
        """Write the step's results; runs inside the step's transaction."""


Planner = Callable[["Context"], Sequence[NewStep]]
Report = Callable[[sqlite3.Connection, str], object]


class _Interrupted(Exception):
    pass


class Context:
    """What a phase sees of the run while it prepares one step."""

    def __init__(self, runner: Runner, step: Step | None) -> None:
        self.runner = runner
        self.step = step
        self.conn = runner.conn
        self.run_id = runner.run_id
        self.cfg = runner.cfg
        self.home = runner.home
        self.clock = runner.clock

    def backends(self, role: str) -> list[Backend]:
        """The role's backends in fallback order."""
        available = self.runner.backends
        names = self.cfg["roles"][role]["backends"]
        return [available[name] for name in names if name in available]

    def call(
        self,
        role: str,
        prompt: str,
        schema: dict,
        *,
        part: str = "",
        input_ref: str | None = None,
    ) -> dict:
        """The answer to one call of this step: the stored one if the call was
        already answered, otherwise a new call through the dispatcher's retry
        and fallback policy."""
        key = steps.call_key(self.step.key, part)
        stored = steps.stored_output(self.conn, self.run_id, key)
        if stored is not None:
            return stored
        result = self.runner.dispatcher.call(
            role,
            self.backends(role),
            lambda backend: self.attempt(key, role, backend, prompt, schema, input_ref),
            self.wait_until,
            self.clock.now,
        )
        return result.output

    def attempt(
        self,
        key: str,
        role: str,
        backend: Backend,
        prompt: str,
        schema: dict,
        input_ref: str | None = None,
    ) -> CallResult:
        """One recorded call: the row is written before it and the raw output as
        soon as it returns. No call starts once a limit is reached or the run is
        told to stop. Missing usage is estimated, and the output is checked
        against the schema whatever the backend's structured-output option
        did."""
        if self.runner.stopping:
            raise _Interrupted
        self.runner.budget.check(self.step.phase if self.step else "")
        session_id = str(uuid.uuid4()) if backend.accepts_session_id else None
        call_id = steps.begin_call(
            self.conn,
            self.run_id,
            key,
            role,
            backend.name,
            self.runner.now(),
            input_ref,
            session_id,
        )
        started = self.clock.monotonic()
        tools = list(self.cfg["tools"][role])
        try:
            result = backend.call(role, prompt, schema, tools, session_id=session_id)
        except Exception as exc:
            seconds = self.clock.monotonic() - started
            steps.end_call(
                self.conn, call_id, self.runner.now(), seconds, error=_describe(exc)
            )
            raise
        seconds = self.clock.monotonic() - started
        result = _with_usage(result, prompt)
        problems = "; ".join(validate(result.output, schema))
        steps.end_call(
            self.conn,
            call_id,
            self.runner.now(),
            seconds,
            result=result,
            error=problems or None,
        )
        if problems:
            raise SchemaError(problems)
        return result

    def wait_until(self, until: datetime, notice: dict) -> None:
        """Wait for a provider usage limit to reset (5.3). The run is `waiting`
        meanwhile, with the reset time and notice on its row, and the time does
        not count toward the wall-clock limit (1.2)."""
        runner = self.runner
        runner.save_clock()
        self.conn.execute(
            "UPDATE runs SET status = 'waiting', wait_until = ?, wait_notice = ?"
            " WHERE id = ?",
            (until.isoformat(timespec="seconds"), json.dumps(notice), self.run_id),
        )
        log(
            f"waiting until {until.isoformat(timespec='seconds')}: {json.dumps(notice)}"
        )
        runner.wall.pause()
        try:
            while not runner.stopping:
                remaining = (until - self.clock.now()).total_seconds()
                if remaining <= 0:
                    break
                self.clock.sleep(min(remaining, WAIT_POLL_SECONDS))
        finally:
            runner.wall.resume()
            self.conn.execute(
                "UPDATE runs SET status = 'running', wait_until = NULL,"
                " wait_notice = NULL WHERE id = ?",
                (self.run_id,),
            )
        if runner.stopping:
            raise _Interrupted


def _with_usage(result: CallResult, prompt: str) -> CallResult:
    """The result with usage the backend did not report estimated as
    `ceil(utf8 bytes / 4)` of the prompt and the output, and flagged (1.2)."""
    if result.tokens_in is not None and result.tokens_out is not None:
        return result
    return replace(
        result,
        tokens_in=(
            estimate_tokens(prompt) if result.tokens_in is None else result.tokens_in
        ),
        tokens_out=(
            estimate_tokens(json.dumps(result.output))
            if result.tokens_out is None
            else result.tokens_out
        ),
        usage_estimated=True,
    )


def _describe(exc: BaseException) -> str:
    if isinstance(exc, DreamingError | BackendError):
        return exc.message
    return f"{type(exc).__name__}: {exc}"


class Runner:
    """Runs a dream's steps until none is ready, a limit is reached or it is told
    to stop."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        home: Path,
        run_id: str,
        cfg: dict,
        *,
        phases: Mapping[str, Phase],
        backends: Mapping[str, Backend],
        planner: Planner | None = None,
        report: Report | None = None,
        clock: Any = None,
    ) -> None:
        self.conn = conn
        self.home = home
        self.run_id = run_id
        self.cfg = cfg
        self.phases = dict(phases)
        self.backends = dict(backends)
        self.planner = planner
        self.report = report
        self.clock = clock or SystemClock()
        spent = conn.execute(
            "SELECT active_seconds FROM runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
        self.wall = WallClock(spent, self.clock.monotonic)
        self.budget = Budget(conn, run_id, cfg["budget"], self.wall)
        self.dispatcher = Dispatcher(cfg["thresholds"])
        # Steps whose input failed, by the backend it failed on; given another
        # chance when the breaker disables that backend.
        self.failed_on: dict[str, set[str]] = {}
        self.stopping = False

    def stop(self) -> None:
        """Stop before the next step or call; nothing running is killed."""
        self.stopping = True

    def now(self) -> str:
        return self.clock.now().isoformat(timespec="seconds")

    def save_clock(self) -> None:
        self.conn.execute(
            "UPDATE runs SET active_seconds = ? WHERE id = ?",
            (self.wall.spent, self.run_id),
        )

    def execute(self) -> str:
        """Run to the end and return the final status. The report is written in
        the exit path whatever the outcome (10.1)."""
        steps.recover(self.conn, self.run_id, self.now())
        error = None
        try:
            status = self._loop()
        except Partial as exc:
            status, error = "partial", exc.message
        except _Interrupted:
            status = "interrupted"
        except Exception as exc:
            traceback.print_exc()
            status, error = "interrupted", _describe(exc)
        self._end(status, error)
        if self.report is not None:
            try:
                self.report(self.conn, self.run_id)
            except Exception:
                traceback.print_exc()
        log(f"dream {self.run_id} {status}" + (f": {error}" if error else ""))
        return status

    def _loop(self) -> str:
        loaded = steps.load(self.conn, self.run_id)
        if not loaded and self.planner is not None:
            planned = self.planner(Context(self, None))
            with store.transaction(self.conn):
                steps.add(self.conn, self.run_id, planned)
        exhausted = self._exhausted_before(loaded)
        while not self.stopping:
            self._retry_tripped()
            step = steps.next_ready(self.conn, self.run_id)
            if step is None:
                self._check_blocked()
                return "budget_exhausted" if exhausted else "complete"
            phase = self.phases.get(step.phase)
            if phase is None:
                raise DreamingError(
                    f"no handler for the {step.phase} phase ({step.key})"
                )
            try:
                if step.phase in steps.MODEL_PHASES:
                    self.budget.check(step.phase)
                self._step(step, phase)
            except LimitReached as limit:
                exhausted = exhausted or limit.exhausted
                with store.transaction(self.conn):
                    steps.finish(
                        self.conn, self.run_id, step.key, "skipped", limit.reason
                    )
                    self.save_clock()
                log(f"{step.key} skipped: {limit.reason}")
        raise _Interrupted

    def _exhausted_before(self, loaded: list[Step]) -> bool:
        """A resumed run whose earlier part already stopped model work at a limit."""
        skipped = any(
            s.state == "skipped" and s.phase in steps.MODEL_PHASES for s in loaded
        )
        if not skipped:
            return False
        try:
            self.budget.check("")
        except LimitReached:
            return True
        return False

    def _step(self, step: Step, phase: Phase) -> None:
        steps.begin(self.conn, self.run_id, step.key)
        log(f"{step.key} started")
        try:
            prepared = phase.prepare(Context(self, step), step)
        except Partial:
            steps.finish(self.conn, self.run_id, step.key, "pending")
            raise
        except InputFailed as failed:
            self.failed_on.setdefault(failed.backend, set()).add(step.key)
            with store.transaction(self.conn):
                steps.finish(self.conn, self.run_id, step.key, "failed", failed.message)
                self.save_clock()
            log(f"{step.key} failed: {failed.message}")
            return
        with store.transaction(self.conn):
            outcome = phase.apply(self.conn, step, prepared)
            steps.add(self.conn, self.run_id, outcome.follow_up)
            steps.finish(
                self.conn, self.run_id, step.key, outcome.state, outcome.reason
            )
            self.save_clock()
        log(f"{step.key} {outcome.state}")

    def _retry_tripped(self) -> None:
        """Inputs that failed on a backend the breaker has since disabled are
        pending again, for the role's next backend (design, Roles and calls)."""
        while self.dispatcher.tripped:
            backend = self.dispatcher.tripped.pop()
            for key in sorted(self.failed_on.pop(backend, ())):
                steps.finish(self.conn, self.run_id, key, "pending")
                log(f"{key} pending again: {backend} is disabled")

    def _check_blocked(self) -> None:
        blocked = [
            s.key for s in steps.load(self.conn, self.run_id) if s.state == "pending"
        ]
        if blocked:
            raise DreamingError(f"steps {blocked} wait on each other and cannot run")

    def _end(self, status: str, error: str | None) -> None:
        ended = None if status == "interrupted" else self.now()
        self.conn.execute(
            "UPDATE runs SET status = ?, error = ?, ended_at = ?, active_seconds = ?,"
            " wait_until = NULL, wait_notice = NULL WHERE id = ?",
            (status, error, ended, self.wall.spent, self.run_id),
        )


# Starting and resuming (1.1, 1.6, 5.4)


@dataclass
class Started:
    run_id: str
    resumed: bool
    process: subprocess.Popen
    selection: dict
    overrides: dict


def _running_dream(home: Path) -> str:
    path = store.store_path(home)
    if path.exists():
        conn = store.connect(path)
        try:
            row = conn.execute(
                "SELECT id FROM runs WHERE status IN ('running', 'waiting')"
                " ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            return f"dream {row['id']} is already running"
    return "another dream is already running"


def _unfinished(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM runs WHERE status IN {store.UNFINISHED_RUN_STATUSES}"
        " ORDER BY started_at DESC LIMIT 1"
    ).fetchone()


def _new_run(
    conn: sqlite3.Connection,
    home: Path,
    cfg: dict,
    selection: dict,
    overrides: dict,
    requester: str,
) -> str:
    effective = config.apply_overrides(cfg, overrides)
    run_id = new_run_id(SystemClock().now())
    try:
        snapshot = config.write_snapshot(home, run_id, cfg)
    except BaseException:
        shutil.rmtree(home / "runs" / run_id, ignore_errors=True)
        raise
    create_run(
        conn,
        run_id,
        effective,
        requester=requester,
        selection=selection,
        overrides=overrides,
        snapshot_path=str(snapshot),
    )
    return run_id


def _invalid_resume(message: str) -> DreamingError:
    return DreamingError(message, USAGE_ERROR, "invalid_override")


def _resume(
    conn: sqlite3.Connection, row: sqlite3.Row, selection: dict, overrides: dict
) -> dict:
    """Validate a resume request and mark the run running again. Only backend
    choices may change; the run keeps its selection, budget and overrides."""
    run_id = row["id"]
    if selection:
        raise _invalid_resume(f"dream {run_id} is resumed with its own selection")
    extra = sorted(set(overrides) - {"roles"})
    if extra:
        raise _invalid_resume(
            f"dream {run_id} is resumed; only backend choices may change, not {extra}"
        )
    if row["revision"] != versions.revision():
        raise DreamingError(
            f"dream {run_id} started on revision {row['revision']}, but this is "
            f"{versions.revision()}; run it with that version or discard it (7.3)",
            FAILURE,
            "wrong_revision",
        )
    merged = json.loads(row["overrides"])
    if overrides:
        merged["roles"] = {**merged.get("roles", {}), **overrides["roles"]}
    config.apply_overrides(run_config(row), merged)
    if row["status"] in ("running", "waiting"):
        # The lock was free, so the process that ran it is gone.
        kill_survivors(row["process_group"])
    conn.execute(
        "UPDATE runs SET status = 'running', overrides = ?, error = NULL,"
        " wait_until = NULL, wait_notice = NULL WHERE id = ?",
        (json.dumps(merged), run_id),
    )
    return merged


def _spawn(home: Path, run_id: str, fd: int, child: Sequence[str]) -> subprocess.Popen:
    """The detached child: a new session that inherits the lock descriptor."""
    path = home / "runs" / run_id / "run.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as output:
        return subprocess.Popen(
            [*child, run_id, "--lock-fd", str(fd)],
            pass_fds=(fd,),
            start_new_session=True,
            cwd=home,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )


def start(
    home: Path,
    cfg: dict,
    *,
    selection: dict | None = None,
    overrides: dict | None = None,
    requester: str = "user",
    child: Sequence[str] | None = None,
) -> Started:
    """Start a dream, or resume the unfinished one, in a detached process (1.1,
    1.6). Exits 4 without changing anything when a dream is running."""
    selection = selection or {}
    overrides = overrides or {}
    fd = acquire_lock(home)
    if fd is None:
        raise DreamingError(_running_dream(home), SKIPPED, "already_running")
    try:
        conn = store.open_store(store.store_path(home))
        try:
            with store.transaction(conn):
                row = _unfinished(conn)
                if row is None:
                    run_id = _new_run(conn, home, cfg, selection, overrides, requester)
                else:
                    run_id = row["id"]
                    overrides = _resume(conn, row, selection, overrides)
                    selection = json.loads(row["selection"])
            try:
                process = _spawn(home, run_id, fd, child or CHILD)
            except OSError as exc:
                conn.execute(
                    "UPDATE runs SET status = 'interrupted', error = ? WHERE id = ?",
                    (f"could not start: {exc}", run_id),
                )
                raise DreamingError(f"could not start dream {run_id}: {exc}") from exc
            conn.execute(
                "UPDATE runs SET process_group = ? WHERE id = ?", (process.pid, run_id)
            )
        finally:
            conn.close()
    finally:
        os.close(fd)
    return Started(run_id, row is not None, process, selection, overrides)


# Dry run (1.7, 5.5)


def dry_run(
    home: Path,
    cfg: dict,
    *,
    selection: dict | None = None,
    overrides: dict | None = None,
    apsis: sessions.Apsis | None = None,
    now: datetime | None = None,
) -> dict:
    """What a start would read and use, with no model call and no change to any
    file or row: the store is read through an in-memory copy, and backends are
    shown from `--version` and the probe cache."""
    cfg = config.apply_overrides(cfg, overrides or {})
    conn = store.memory_copy(store.store_path(home))
    try:
        unfinished = _unfinished(conn)
        chosen = sessions.select_sessions(
            conn,
            apsis or sessions.Apsis.from_config(cfg, home),
            cfg,
            sessions.SelectionRequest.from_dict(selection or {}),
            home=home,
            now=now,
        )
    finally:
        conn.close()
    statuses: dict[str, probe.ProbeResult] = {}
    roles = {}
    for role in config.ROLES:
        roles[role] = []
        for name in cfg["roles"][role]["backends"]:
            if name not in statuses:
                statuses[name] = probe.cached_status(home, cfg, name)
            result = statuses[name]
            roles[role].append(
                {
                    "backend": name,
                    "status": result.status,
                    "usable": result.usable,
                    "version": result.version,
                    "reason": result.reason or "; ".join(result.problems()),
                }
            )
    return {
        "resumes": unfinished["id"] if unfinished else None,
        "sessions": [
            {
                "source": s.source,
                "id": s.id,
                "project": s.attribution.project,
                "started_at": s.attribution.started_at,
                "subject": s.attribution.subject,
                "from_entry": s.start,
                "entries": len(s.entries()),
            }
            for s in chosen.sessions
        ],
        "skipped": [
            {"source": s.source, "id": s.id, "reason": s.reason} for s in chosen.skipped
        ],
        "roles": roles,
        "budget": cfg["budget"],
    }


# The detached child

# Handlers by phase, the planner that creates a new run's first steps, the
# callable backends and the report. The modules for each phase fill these in.
PHASES: dict[str, Phase] = {}
BACKENDS: dict[str, Backend] = {}
PLANNER: Planner | None = None
REPORT: Report | None = None


def child_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dreaming.run")
    parser.add_argument("run_id")
    parser.add_argument("--lock-fd", type=int, required=True)
    args = parser.parse_args(argv)
    # The parent took the lock on this descriptor; nothing this process starts
    # may inherit it.
    os.set_inheritable(args.lock_fd, False)
    try:
        fcntl.flock(args.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"dream {args.run_id}: the lock is held by another process")
        return SKIPPED
    home = config.dreaming_home()
    conn = store.open_store(store.store_path(home))
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (args.run_id,)).fetchone()
    if row is None:
        log(f"dream {args.run_id} does not exist")
        return FAILURE
    runner = Runner(
        conn,
        home,
        args.run_id,
        run_config(row),
        phases=PHASES,
        backends=BACKENDS,
        planner=PLANNER,
        report=REPORT,
    )
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: runner.stop())
    log(f"dream {args.run_id} running (pid {os.getpid()}, revision {row['revision']})")
    status = runner.execute()
    conn.close()
    return exit_code(status)


if __name__ == "__main__":
    sys.exit(child_main())
