"""Stable step keys, durable call responses, and transactional result application."""

import json
import math
import uuid
from dataclasses import dataclass

from dreaming.knowledge import record_call_inputs
from dreaming.redact import redact
from dreaming.store import Store


@dataclass(frozen=True)
class Step:
    key: str
    phase: str
    depends_on: tuple[str, ...] = ()
    input_ref: str | None = None


@dataclass(frozen=True)
class CallResult:
    raw_output: str
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0
    usage_estimated: bool = False
    session_ref: str | None = None
    outcome: str = "success"
    error: str | None = None


def insert_steps(conn, run_id: str, steps: tuple[Step, ...]) -> None:
    for step in steps:
        if not step.key or not step.phase or any(c in step.key + step.phase for c in "\r\n"):
            raise ValueError("Step keys and phases must be nonempty single lines")
        if conn.execute(
            "SELECT 1 FROM steps WHERE run_id=? AND key=?", (run_id, step.key)
        ).fetchone():
            raise ValueError(f"Step key already exists: {step.key}")
        conn.execute(
            "INSERT INTO steps(run_id,key,phase,depends_on,input_ref) VALUES (?,?,?,?,?)",
            (run_id, step.key, step.phase, json.dumps(step.depends_on), step.input_ref),
        )
    graph = {
        key: json.loads(dependencies)
        for key, dependencies in conn.execute(
            "SELECT key,depends_on FROM steps WHERE run_id=?", (run_id,)
        )
    }
    visited, visiting = set(), set()

    def visit(key):
        if key not in graph:
            raise ValueError(f"Missing step dependency: {key}")
        if key in visiting:
            raise ValueError(f"Step dependency cycle: {key}")
        if key not in visited:
            visiting.add(key)
            for dependency in graph[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

    for key in graph:
        visit(key)


class Steps:
    def __init__(self, store: Store, run_id: str):
        self.store, self.run_id = store, run_id

    def list(self) -> list[dict]:
        cursor = self.store.connection.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY rowid", (self.run_id,)
        )
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor]

    def add(self, *steps: Step) -> None:
        with self.store.transaction() as conn:
            insert_steps(conn, self.run_id, steps)

    def next(self) -> Step | None:
        rows = self.list()
        done = {s["key"] for s in rows if s["state"] == "done"}
        for row in rows:
            dependencies = tuple(json.loads(row["depends_on"]))
            if row["state"] in {"pending", "running"} and set(dependencies) <= done:
                return Step(row["key"], row["phase"], dependencies, row["input_ref"])
        return None

    def start(self, key: str) -> None:
        result = self.store.connection.execute(
            "UPDATE steps SET state='running' WHERE run_id=? AND key=? "
            "AND state IN ('pending','running')",
            (self.run_id, key),
        )
        if not result.rowcount:
            raise ValueError(f"Step is not runnable: {key}")

    def begin_call(
        self,
        key: str,
        *,
        role: str,
        backend: str,
        model: str | None = None,
        session_ref: str | None = None,
        input_ref: str | None = None,
        sessions: tuple[tuple[str, str], ...] = (),
    ) -> str:
        call_id = str(uuid.uuid4())
        with self.store.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM calls WHERE run_id=? AND outcome IS NULL", (self.run_id,)
            ).fetchone():
                raise ValueError("A call is already in flight")
            result = conn.execute(
                "UPDATE steps SET attempts=attempts+1 WHERE run_id=? AND key=? AND state='running'",
                (self.run_id, key),
            )
            if not result.rowcount:
                raise ValueError(f"Step is not running: {key}")
            attempt = conn.execute(
                "SELECT attempts FROM steps WHERE run_id=? AND key=?", (self.run_id, key)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO calls(id,run_id,task_key,attempt,role,backend,model,session_ref,"
                "input_ref) VALUES (?,?,?,?,?,?,?,?,?)",
                (call_id, self.run_id, key, attempt, role, backend, model, session_ref, input_ref),
            )
        # The row exists before lineage checks and dispatch; a rejected input never calls a model.
        try:
            record_call_inputs(self.store, call_id, sessions)
        except Exception as exc:
            self.record_call(call_id, CallResult("", outcome="input_rejected", error=str(exc)))
            raise
        return call_id

    def record_call(self, call_id: str, result: CallResult) -> None:
        if any(type(n) is not int or n < 0 for n in (result.tokens_in, result.tokens_out)):
            raise ValueError("Call usage must be nonnegative integers")
        if not math.isfinite(result.seconds) or result.seconds < 0:
            raise ValueError("Call duration must be finite and nonnegative")
        updated = self.store.connection.execute(
            "UPDATE calls SET raw_output=?,tokens_in=?,tokens_out=?,seconds=?,usage_estimated=?,"
            "session_ref=COALESCE(?,session_ref),outcome=?,error=? "
            "WHERE id=? AND run_id=? AND outcome IS NULL",
            (
                redact(result.raw_output),
                result.tokens_in,
                result.tokens_out,
                result.seconds,
                int(result.usage_estimated),
                result.session_ref,
                result.outcome,
                redact(result.error) if result.error else None,
                call_id,
                self.run_id,
            ),
        )
        if not updated.rowcount:
            raise ValueError("Call not found or already checkpointed")

    def saved_output(self, key: str) -> str | None:
        row = self.store.connection.execute(
            "SELECT raw_output FROM calls WHERE run_id=? AND task_key=? AND outcome='success' "
            "ORDER BY attempt DESC LIMIT 1",
            (self.run_id, key),
        ).fetchone()
        return row[0] if row else None

    def apply(self, key: str, raw: str, apply, followups: tuple[Step, ...] = ()) -> None:
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM steps WHERE run_id=? AND key=?", (self.run_id, key)
            ).fetchone()
            if row is None or row[0] == "done":
                raise ValueError(f"Step already applied or missing: {key}")
            extra = apply(conn, raw) or ()
            insert_steps(conn, self.run_id, (*followups, *extra))
            conn.execute(
                "UPDATE steps SET state='done',result=? WHERE run_id=? AND key=?",
                (redact(raw), self.run_id, key),
            )
