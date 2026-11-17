"""The `steps` table and the `calls` records behind it (design, Steps and plan view).

The steps table is the only record of what a dream will do and has done. Keys
are assigned by the runner and stable (`read:<batch>`, `research:<hash>`, ...).
A step's follow-up steps are added in the same transaction that applies its
results and marks it done. A `calls` row is written before each model call and
gets the raw output as soon as the call returns, so a resumed run applies that
output instead of asking again.

Functions here run in the caller's transaction, or commit at once in autocommit.
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from dreaming.backends import CallResult

PHASES = ("read", "review", "research", "author", "adopt")
MODEL_PHASES = ("read", "review", "research", "author")
FINISHED = ("done", "failed", "skipped")


@dataclass(frozen=True)
class NewStep:
    key: str
    phase: str
    depends_on: tuple[str, ...] = ()
    input_ref: str | None = None


@dataclass(frozen=True)
class Step:
    run: str
    key: str
    phase: str
    depends_on: tuple[str, ...]
    state: str
    attempts: int
    input_ref: str | None
    reason: str | None


def _step(row: sqlite3.Row) -> Step:
    return Step(
        row["run"],
        row["key"],
        row["phase"],
        tuple(json.loads(row["depends_on"])),
        row["state"],
        row["attempts"],
        row["input_ref"],
        row["reason"],
    )


def load(conn: sqlite3.Connection, run_id: str) -> list[Step]:
    """The run's steps in the order they were added."""
    rows = conn.execute("SELECT * FROM steps WHERE run = ? ORDER BY seq", (run_id,))
    return [_step(row) for row in rows]


def add(conn: sqlite3.Connection, run_id: str, new: Iterable[NewStep]) -> None:
    """Add steps after the existing ones. A key already present is left as it is;
    every dependency must name an existing step or one added here."""
    new = list(new)
    known = {
        row[0] for row in conn.execute("SELECT key FROM steps WHERE run = ?", (run_id,))
    }
    known.update(step.key for step in new)
    for step in new:
        missing = [key for key in step.depends_on if key not in known]
        if missing:
            raise ValueError(f"step {step.key} depends on unknown steps {missing}")
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM steps WHERE run = ?", (run_id,)
    ).fetchone()[0]
    for offset, step in enumerate(new, start=1):
        conn.execute(
            "INSERT OR IGNORE INTO steps (run, key, phase, depends_on, input_ref, seq)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                step.key,
                step.phase,
                json.dumps(list(step.depends_on)),
                step.input_ref,
                seq + offset,
            ),
        )


def wait_for(
    conn: sqlite3.Connection, run_id: str, key: str, keys: Iterable[str]
) -> None:
    """Make a pending step also wait for `keys`, steps added later in the same
    transaction, as a currency review's adoption waits for authoring that its
    research steps plan as they go."""
    row = conn.execute(
        "SELECT depends_on FROM steps WHERE run = ? AND key = ? AND state = 'pending'",
        (run_id, key),
    ).fetchone()
    if row is None:
        return
    depends_on = json.loads(row[0])
    depends_on += [k for k in keys if k not in depends_on]
    conn.execute(
        "UPDATE steps SET depends_on = ? WHERE run = ? AND key = ?",
        (json.dumps(depends_on), run_id, key),
    )


def next_ready(conn: sqlite3.Connection, run_id: str) -> Step | None:
    """The first pending step whose dependencies have all finished.

    A failed or skipped dependency counts as finished, so a failed Read batch
    (4.7) or a step stopped by a limit does not hold back the steps after it.
    """
    loaded = load(conn, run_id)
    state = {step.key: step.state for step in loaded}
    for step in loaded:
        if step.state == "pending" and all(
            state[key] in FINISHED for key in step.depends_on
        ):
            return step
    return None


def begin(conn: sqlite3.Connection, run_id: str, key: str) -> None:
    conn.execute(
        "UPDATE steps SET state = 'running', attempts = attempts + 1"
        " WHERE run = ? AND key = ?",
        (run_id, key),
    )


def finish(
    conn: sqlite3.Connection,
    run_id: str,
    key: str,
    state: str,
    reason: str | None = None,
) -> None:
    conn.execute(
        "UPDATE steps SET state = ?, reason = ? WHERE run = ? AND key = ?",
        (state, reason, run_id, key),
    )


def recover(conn: sqlite3.Connection, run_id: str, now: str) -> None:
    """After an interruption: running steps are pending again, and calls that
    never returned are marked interrupted."""
    conn.execute(
        "UPDATE steps SET state = 'pending' WHERE run = ? AND state = 'running'",
        (run_id,),
    )
    conn.execute(
        "UPDATE calls SET outcome = 'interrupted', ended_at = ?"
        " WHERE run = ? AND ended_at IS NULL",
        (now, run_id),
    )


# Calls


def call_key(step_key: str, part: str = "") -> str:
    """Identifies one call of a step; a step making several calls names each part."""
    return f"{step_key}#{part}" if part else step_key


def stored_output(conn: sqlite3.Connection, run_id: str, key: str) -> dict | None:
    """The answer already received for this call, if any."""
    row = conn.execute(
        "SELECT raw_output FROM calls WHERE run = ? AND step_key = ? AND outcome = 'ok'"
        " ORDER BY id DESC LIMIT 1",
        (run_id, key),
    ).fetchone()
    return None if row is None else json.loads(row[0])


def answered_by(
    conn: sqlite3.Connection, run_id: str, key: str
) -> tuple[str | None, str | None]:
    """The backend and model whose answer to this call was kept (5.6)."""
    row = conn.execute(
        "SELECT backend, model FROM calls WHERE run = ? AND step_key = ?"
        " AND outcome = 'ok' ORDER BY id DESC LIMIT 1",
        (run_id, key),
    ).fetchone()
    return (None, None) if row is None else (row["backend"], row["model"])


def begin_call(
    conn: sqlite3.Connection,
    run_id: str,
    key: str,
    role: str,
    backend: str,
    now: str,
    input_ref: str | None = None,
    harness_session_id: str | None = None,
) -> int:
    """Write the calls row before the call is made, with the harness session id
    when one is chosen beforehand, so the call's session is known even if the
    process dies during it (1.5)."""
    attempt = conn.execute(
        "SELECT COUNT(*) + 1 FROM calls WHERE run = ? AND step_key = ?",
        (run_id, key),
    ).fetchone()[0]
    cursor = conn.execute(
        "INSERT INTO calls (run, step_key, attempt, role, backend, input_ref,"
        " harness_session_id, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, key, attempt, role, backend, input_ref, harness_session_id, now),
    )
    return cursor.lastrowid


def end_call(
    conn: sqlite3.Connection,
    call_id: int,
    now: str,
    seconds: float,
    result: CallResult | None = None,
    error: str | None = None,
) -> None:
    """Store the raw output, or the error, as soon as the call returns. An
    output with an error did not match its schema: it is kept and counted, but
    its outcome is `invalid`, so a resume never applies it."""
    if result is None:
        conn.execute(
            "UPDATE calls SET outcome = 'error', error = ?, seconds = ?, ended_at = ?"
            " WHERE id = ?",
            (error, seconds, now, call_id),
        )
        return
    conn.execute(
        "UPDATE calls SET outcome = ?, error = ?, raw_output = ?, tokens_in = ?,"
        " tokens_out = ?, usage_estimated = ?, session_ref = ?, model = ?,"
        " seconds = ?, ended_at = ? WHERE id = ?",
        (
            "invalid" if error else "ok",
            error,
            json.dumps(result.output),
            result.tokens_in,
            result.tokens_out,
            int(result.usage_estimated),
            result.session_ref,
            result.model,
            seconds,
            now,
            call_id,
        ),
    )


# Rejections (6.2, 7.5, 11.6)


def record_rejection(
    conn: sqlite3.Connection,
    run_id: str,
    step_key: str,
    what: str,
    subject: str | None,
    reason: str,
    detail: dict | None = None,
) -> None:
    """Record a model's observation or operation the program did not apply, and
    why, for the report and later dreams' run records."""
    conn.execute(
        "INSERT INTO rejections (run, step_key, what, subject, reason, detail,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S+00:00'))",
        (run_id, step_key, what, subject, reason, json.dumps(detail or {})),
    )
