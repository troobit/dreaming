"""A dream's steps rendered as a rune task file (design, Steps and plan view).

`dream status --plan` writes `runs/<id>/tasks-dream.md` for reading with
`rune list`. The file is output only: the program never reads it back, so
editing it changes nothing and the next render replaces it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from dreaming import steps
from dreaming.steps import Step

PLAN_NAME = "tasks-dream.md"

# rune marks: `[x]` completed, `[-]` in progress, `[ ]` pending. Failed and
# skipped steps are finished, so they are completed with their state named.
_MARKS = {"done": "x", "failed": "x", "skipped": "x", "running": "-", "pending": " "}


def _line(text: str) -> str:
    return " ".join(text.split())


def render(run_id: str, loaded: list[Step]) -> str:
    lines = [f"# Dream {run_id}", ""]
    number = 0
    for phase in steps.PHASES:
        in_phase = [step for step in loaded if step.phase == phase]
        if not in_phase:
            continue
        lines += [f"## {phase.capitalize()}", ""]
        for step in in_phase:
            number += 1
            title = step.key
            if step.state in ("failed", "skipped"):
                title += f" ({step.state})"
            lines.append(f"- [{_MARKS[step.state]}] {number}. {title}")
            if step.depends_on:
                lines.append(f"  - Depends on: {', '.join(step.depends_on)}")
            if step.attempts > 1:
                lines.append(f"  - Attempts: {step.attempts}")
            if step.reason:
                lines.append(f"  - Reason: {_line(step.reason)}")
        lines.append("")
    return "\n".join(lines)


def write(home: Path, conn: sqlite3.Connection, run_id: str) -> Path:
    path = home / "runs" / run_id / PLAN_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(run_id, steps.load(conn, run_id)))
    return path
