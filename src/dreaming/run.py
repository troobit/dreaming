"""Single-worker runs, continuous lock handoff, and durable step execution."""

import argparse
import fcntl
import importlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from dreaming.budget import RunBudget
from dreaming.config import (
    ROLES,
    BackendConfig,
    Budget,
    Config,
    ConfigError,
    Thresholds,
    ensure_home,
    require_external,
    write_snapshot,
)
from dreaming.redact import redact
from dreaming.sessions import SessionError
from dreaming.steps import CallResult, Step, StepDeferred, Steps, insert_steps
from dreaming.store import Store, snapshot_connection


def exit_code(status: str) -> int:
    return {
        "complete": 0,
        "budget_exhausted": 0,
        "discarded": 0,
        "partial": 5,
        "config_error": 2,
        "already_running": 4,
    }.get(status, 1)


def inspect_run(home: Path, run_id: str | None = None, *, unfinished: bool = False) -> dict | None:
    """No migration, file creation, WAL checkpoint, or write connection for inspection."""
    path = require_external(home / "dreaming.db")
    if not path.exists():
        return None
    conn = snapshot_connection(path)
    try:
        conn.row_factory = sqlite3.Row
        where, args = ("WHERE id=?", (run_id,)) if run_id else ("", ())
        if unfinished:
            where = "WHERE status IN ('running','waiting','interrupted','partial') "
            where += "AND snapshot_path IS NOT NULL"
        row = conn.execute(
            f"SELECT * FROM runs {where} ORDER BY rowid DESC LIMIT 1", args
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


class AlreadyRunning(RuntimeError):
    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"Dream already running: {run_id}")


class RunLock:
    def __init__(self, home: Path):
        self.home, self.fd = require_external(home), None

    def __enter__(self):
        if not self.home.exists():
            ensure_home(self.home)
        path = require_external(self.home / "lock")
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            record = inspect_run(self.home, unfinished=True)
            raise AlreadyRunning(record["id"] if record else "unknown") from None
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            # LOCK_UN would also unlock the detached child's shared open-file description.
            os.close(self.fd)
            self.fd = None


def kill_survivors(group: int | None) -> None:
    if group is None:
        return
    if group <= 1 or group == os.getpgrp():
        raise ConfigError("Refusing an invalid or invoking process group")
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def snapshot_config(path: Path) -> Config:
    raw = json.loads((path / "config.json").read_text())
    for key in ("source", "instruction_repo", "names_file"):
        if raw.get(key) is not None:
            raw[key] = Path(raw[key])
    raw["budget"] = Budget(**raw["budget"])
    raw["thresholds"] = Thresholds(**raw["thresholds"])
    raw["backends"] = {k: BackendConfig(**v) for k, v in raw["backends"].items()}
    for key in ("roles", "tools"):
        raw[key] = {k: tuple(v) for k, v in raw[key].items()}
    for key in ("classification", "knowledge_folders"):
        raw[key] = tuple(raw[key])
    raw["currency_targets"] = tuple(raw.get("currency_targets", ()))
    return Config(**raw)


@dataclass(frozen=True)
class RunRecord:
    id: str
    revision: str
    config: Config
    selection: list
    overrides: dict
    resumed: bool


class RunSession:
    """Acquire before opening a writable database; the caller owns the entire run."""

    def __init__(self, home: Path):
        self.lock = RunLock(home)

    def __enter__(self):
        self.lock.__enter__()
        try:
            self.store = Store(self.lock.home)
        except BaseException:
            self.lock.__exit__()
            raise
        return self

    def __exit__(self, *exc):
        self.store.close()
        self.lock.__exit__(*exc)

    def prepare(
        self,
        config: Config,
        *,
        revision: str,
        selection: list | None = None,
        overrides: dict | None = None,
        backend_overrides: dict | None = None,
        steps: tuple[Step, ...] = (),
        requester: str = "user",
        allow_new: bool = True,
    ) -> RunRecord:
        old = inspect_run(self.lock.home, unfinished=True)
        if old:
            if overrides or selection is not None or steps:
                raise ConfigError(
                    "Resume accepts only backend overrides; selection and limits are retained"
                )
            saved = snapshot_config(Path(old["snapshot_path"]))
            choices = json.loads(old["statistics"]).get("backend_overrides", {})
            choices.update(backend_overrides or {})
            if any(
                role not in ROLES
                or not isinstance(names, (list, tuple))
                or not names
                or any(name not in saved.backends for name in names)
                for role, names in choices.items()
            ):
                raise ConfigError("Invalid backend overrides")
            saved = replace(
                saved, roles={**saved.roles, **{r: tuple(v) for r, v in choices.items()}}
            )
            active = RunBudget(self.store, old["id"], saved.budget)
            adopted = self.store.connection.execute(
                "SELECT 1 FROM adoption WHERE run_id=? "
                "AND step IN ('merging','merged','deploying','deployed')",
                (old["id"],),
            ).fetchone()
            kill_survivors(old["process_group"])
            if active.exhausted() and not adopted:
                if not allow_new:
                    raise ConfigError("Run budget is exhausted; start a new dream")
                self.store.connection.execute(
                    "UPDATE runs SET status='budget_exhausted',ended_at=? WHERE id=?",
                    (datetime.now(UTC).isoformat(), old["id"]),
                )
                # A fully spent interrupted run is not selected for automatic resumption.
                return self.prepare(config, revision=revision, requester=requester)
            statistics = {
                **json.loads(old["statistics"]),
                "backend_overrides": choices,
                "resumed": True,
            }
            self.store.connection.execute(
                "UPDATE runs SET status=?,process_group=NULL,ended_at=NULL,statistics=? WHERE id=?",
                (
                    "waiting" if old["status"] == "waiting" else "running",
                    json.dumps(statistics),
                    old["id"],
                ),
            )
            return RunRecord(
                old["id"],
                old["revision"],
                saved,
                json.loads(old["selection"]),
                json.loads(old["overrides"]),
                True,
            )
        if backend_overrides:
            raise ConfigError("Use effective configuration for new-run backend choices")
        run_id = str(uuid.uuid4())
        snapshot = write_snapshot(config, self.lock.home, run_id)
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO runs(id,kind,status,revision,requester,started_at,selection,"
                "overrides,budget,snapshot_path) VALUES (?,?,'running',?,?,?,?,?,?,?)",
                (
                    run_id,
                    (overrides or {}).get("kind", "standard"),
                    revision,
                    requester,
                    datetime.now(UTC).isoformat(),
                    json.dumps(selection or []),
                    json.dumps(overrides or {}),
                    json.dumps(config.budget.__dict__),
                    str(snapshot),
                ),
            )
            insert_steps(conn, run_id, steps)
        return RunRecord(run_id, revision, config, selection or [], overrides or {}, False)

    def detach(self, target: str, run_id: str) -> int:
        """Start an importable worker function with (store, run_id), without a shell."""
        read_fd, write_fd = os.pipe()
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "dreaming.run",
                    "--worker",
                    target,
                    "--home",
                    str(self.lock.home),
                    "--run-id",
                    run_id,
                    "--lock-fd",
                    str(self.lock.fd),
                    "--ready-fd",
                    str(read_fd),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                pass_fds=(self.lock.fd, read_fd),
            )
            self.store.connection.execute(
                "UPDATE runs SET process_group=? WHERE id=?", (process.pid, run_id)
            )
            os.write(write_fd, b"1")
            return process.pid
        finally:
            os.close(read_fd)
            os.close(write_fd)


class Engine:
    def __init__(
        self,
        store: Store,
        run_id: str,
        handlers: dict,
        *,
        finalize=None,
        clock=None,
        dispatcher=None,
    ):
        self.store, self.run_id, self.handlers, self.finalize = store, run_id, handlers, finalize
        self.steps = Steps(store, run_id)
        raw = json.loads(
            store.connection.execute("SELECT budget FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        )
        limits = Budget(**{k: v for k, v in raw.items() if k in Budget.__dataclass_fields__})
        self.budget = RunBudget(store, run_id, limits, **({"clock": clock} if clock else {}))
        self.stop_requested = False
        self.dispatcher = dispatcher
        if dispatcher is not None:
            dispatcher.budget = self.budget
            dispatcher.interrupted = lambda: self.stop_requested

    def request_stop(self, *_):
        self.stop_requested = True

    def execute(self) -> int:
        status = "complete"
        signals = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                signals[sig] = signal.signal(sig, self.request_stop)
        try:
            stats = json.loads(
                self.store.connection.execute(
                    "SELECT statistics FROM runs WHERE id=?", (self.run_id,)
                ).fetchone()[0]
            )
            if (
                stats.get("resumed")
                and self.store.connection.execute(
                    "SELECT 1 FROM adoption WHERE run_id=? "
                    "AND step IN ('merging','merged','deploying','deployed')",
                    (self.run_id,),
                ).fetchone()
            ):
                self.store.connection.execute(
                    "UPDATE runs SET notice=? WHERE id=?",
                    ("adoption interrupted; manual recovery required", self.run_id),
                )
                status = "partial"
            else:
                status = self._loop()
        except (KeyboardInterrupt, SystemExit):
            status = "interrupted"
            raise
        except Exception as exc:
            status = "interrupted"
            self.store.connection.execute(
                "UPDATE runs SET notice=? WHERE id=?", (redact(str(exc)), self.run_id)
            )
            raise
        finally:
            for sig, previous in signals.items():
                signal.signal(sig, previous)
            self.budget.checkpoint()
            self.store.connection.execute(
                "UPDATE runs SET status=?,ended_at=?,budget=? WHERE id=?",
                (
                    status,
                    datetime.now(UTC).isoformat(),
                    json.dumps(self.budget.summary()),
                    self.run_id,
                ),
            )
            if self.finalize:
                self.finalize(self.store, self.run_id)
        return exit_code(status)

    def _omit_for_budget(self, handler, step) -> None:
        """Complete a step without a model call; a handler may schedule follow-up steps."""
        omitted = getattr(handler, "omitted", None)
        self.steps.apply(
            step.key,
            '{"omitted":"budget"}',
            lambda conn, output: omitted(conn, step) if omitted else (),
        )

    def _loop(self) -> str:
        exhausted = False
        while step := self.steps.next():
            if self.stop_requested:
                return "interrupted"
            self.budget.checkpoint()
            self.steps.start(step.key)
            raw = self.steps.saved_output(step.key)
            if self.store.connection.execute(
                "SELECT 1 FROM calls WHERE run_id=? AND task_key=? AND outcome IS NULL",
                (self.run_id, step.key),
            ).fetchone():
                self.store.connection.execute(
                    "UPDATE runs SET notice=? WHERE id=?",
                    (f"indeterminate call for {step.key}; no durable response", self.run_id),
                )
                return "partial"
            handler = self.handlers.get(step.phase)
            if handler is None:
                self.store.connection.execute(
                    "UPDATE runs SET notice=? WHERE id=?",
                    (f"No handler for {step.phase}", self.run_id),
                )
                return "partial"
            if raw is None and (
                hasattr(handler, "call")
                or (self.dispatcher is not None and hasattr(handler, "prepare"))
            ):
                exhausted |= self.budget.exhausted()
                context_before = None
                if self.dispatcher is not None and hasattr(handler, "resize"):
                    context_before = self.dispatcher.context_limit(handler.role)
                    handler.resize(context_before)
                try:
                    metadata = handler.prepare(step)
                except StepDeferred as exc:
                    raw = json.dumps({"deferred": str(exc)})
                    self.steps.apply(
                        step.key,
                        raw,
                        lambda conn, output, handler=handler, step=step: handler.apply(
                            conn, step, output
                        ),
                    )
                    continue
                except SessionError as exc:
                    if exc.code not in {"forgotten", "forgotten_input"}:
                        raise
                    self.steps.apply(step.key, '{"omitted":"forgotten"}', lambda conn, output: None)
                    continue
                sessions = metadata.get("sessions", ())
                if not self.budget.can_call(metadata["role"], key=step.key, sessions=sessions):
                    exhausted |= metadata["role"] == "reader" and self.budget.read_limit_reached(
                        step.key, sessions
                    )
                    # Complete a model-free omission so the reviewer can use earlier evidence.
                    self._omit_for_budget(handler, step)
                    continue
                if self.dispatcher is not None:
                    try:
                        response = self.dispatcher.dispatch(step.key, **metadata)
                    except SessionError as exc:
                        if exc.code not in {"forgotten", "forgotten_input"}:
                            raise
                        continue
                else:
                    ref = self.steps.begin_call(step.key, **metadata)
                    response: CallResult = handler.call(step)
                    self.steps.record_call(ref, response)
                self.budget.checkpoint()
                if response.outcome == "context_changed" and context_before is not None:
                    if self.dispatcher.context_limit(handler.role) < context_before:
                        continue
                if response.outcome == "budget_exhausted":
                    exhausted = True
                    self._omit_for_budget(handler, step)
                    continue
                if response.outcome == "interrupted":
                    return "interrupted"
                if response.outcome == "input_rejected":
                    self.steps.apply(step.key, '{"omitted":"forgotten"}', lambda conn, output: None)
                    continue
                if response.outcome == "schema_failure":
                    if hasattr(handler, "failed"):
                        with self.store.transaction() as conn:
                            handler.failed(conn, step, "schema_failure")
                    self.steps.apply(
                        step.key, '{"omitted":"schema_failure"}', lambda conn, output: None
                    )
                    continue
                if response.outcome != "success":
                    self.store.connection.execute(
                        "UPDATE runs SET notice=? WHERE id=?",
                        (redact(response.error or response.outcome), self.run_id),
                    )
                    return "partial"
                raw = self.steps.saved_output(step.key)
            if hasattr(handler, "before_apply"):
                # Source and Git reads must finish before opening a write transaction.
                raw = handler.before_apply(step, raw if raw is not None else "null")
            self.steps.apply(
                step.key,
                raw if raw is not None else "null",
                lambda conn, output, handler=handler, step=step: handler.apply(conn, step, output),
            )
        incomplete = any(
            s["state"] in {"pending", "running"}
            or (s["state"] == "failed" and s["phase"] != "Probe")
            for s in self.steps.list()
        )
        if incomplete:
            return "partial"
        if self.stop_requested:
            return "interrupted"
        return "budget_exhausted" if exhausted or self.budget.exhausted() else "complete"


def worker_main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--lock-fd", required=True, type=int)
    parser.add_argument("--ready-fd", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        # A barrier closes the parent-death window before the process group is recorded.
        if os.read(args.ready_fd, 1) != b"1":
            return 1
        os.close(args.ready_fd)
        os.set_inheritable(args.lock_fd, False)
        module, name = args.worker.split(":", 1)
        with Store(args.home) as store:
            result = getattr(importlib.import_module(module), name)(store, args.run_id)
        return result if isinstance(result, int) else 0
    finally:
        os.close(args.lock_fd)


if __name__ == "__main__":
    raise SystemExit(worker_main())
