"""The dream report (10.x): one HTML page per dream.

`Writer(home, cfg)` is the run engine's report (`run.Report`). In the run's exit
path, whatever the outcome, it writes `<home>/reports/<run-id>.html` (the home is
outside every repository, 6.4), records the path in `runs.report_path` and, for
a dream the user started, opens the page in the default browser (10.1).

The page's sections are in `sections.py`; its stylesheet and shell are vendored
from agentic-coding in `shell.py` (10.5).
"""

import sqlite3
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

from dreaming import inventory
from dreaming.errors import USAGE_ERROR, DreamingError
from dreaming.report import sections, shell

REPORTS = "reports"
# The requester of a dream the user started from a terminal or a skill; a tool
# names itself (14.1).
USER = "user"


def report_path(home: Path, run_id: str) -> Path:
    return home / REPORTS / f"{run_id}.html"


def render(conn: sqlite3.Connection, run_id: str, cfg: dict) -> str:
    """The report page of a dream, from what the store holds now."""
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise DreamingError(f"no record of dream {run_id}", USAGE_ERROR, "not_found")
    shown = []
    for anchor, title, body in sections.SECTIONS:
        html = body(conn, run, cfg)
        if html:
            shown.append((anchor, title, html))
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return shell.page(
        title=f"Dream {run_id}",
        subtitle=sections.subtitle(run),
        metrics=sections.metrics(conn, run),
        cards=sections.cards(conn, run),
        sections=shown,
        footer=(
            f"Generated {generated} · dreaming revision "
            f"<code>{shell.escape(run['revision'])}</code> · find it again with "
            f"<code>dream report {shell.escape(run_id)}</code>."
        ),
    )


def write(conn: sqlite3.Connection, run_id: str, home: Path, cfg: dict) -> Path:
    """Record the run's tooling inventory as it ends (8.x), write the report,
    record where it is and return its location (10.1)."""
    inventory.record(conn, cfg, run_id)
    path = report_path(home, run_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(render(conn, run_id, cfg), encoding="utf-8")
    conn.execute("UPDATE runs SET report_path = ? WHERE id = ?", (str(path), run_id))
    return path


def open_report(path: Path, requester: str, cfg: dict) -> bool:
    """Open the report in the default browser for a dream the user started,
    unless `report.open_browser` is off. A missing or failing browser is logged,
    never raised, so it cannot fail the dream (10.1)."""
    if requester != USER or not cfg["report"]["open_browser"]:
        return False
    return open_in_browser(path)


def open_in_browser(path: Path) -> bool:
    """Open a report in the default browser, reporting rather than raising a
    failure."""
    try:
        opened = webbrowser.open(path.as_uri())
    except Exception as exc:
        print(f"report {path}: the browser failed: {exc}", flush=True)
        return False
    if not opened:
        print(f"report {path}: no browser to open it in", flush=True)
    return opened


class Writer:
    """The run engine's report (`run.Report`): writes the page in the run's exit
    path and opens it for the user's own dreams."""

    def __init__(self, home: Path, cfg: dict) -> None:
        self.home = home
        self.cfg = cfg

    def __call__(self, conn: sqlite3.Connection, run_id: str) -> Path:
        path = write(conn, run_id, self.home, self.cfg)
        row = conn.execute("SELECT requester FROM runs WHERE id = ?", (run_id,))
        open_report(path, row.fetchone()[0], self.cfg)
        return path
